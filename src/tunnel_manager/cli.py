"""Command line interface: `tunnel-manager [command]`."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from typing import List, Optional

from . import __version__, paths

DEFAULT_PORT = 5766

USAGE = """\
tunnel-manager [command] [options]

  (no command)            start the manager in the background (if needed) and open the web UI
  stop                    stop the manager and all tunnels
  serve                   run the manager in the foreground (Ctrl+C stops it and its tunnels)
  list                    list tunnels and their status
  add [opts] SSH-ARGS     add a tunnel to history (--fav: to favourites)
  up NAME|ID              start a tunnel
  down NAME|ID | --all    stop a tunnel / all tunnels
  fav NAME|ID             add a tunnel to favourites
  unfav NAME|ID           remove a tunnel from favourites
  rm NAME|ID [-y]         delete a tunnel
  logs NAME|ID [-f]       show (and follow) a tunnel's ssh log
  import-history          import tunnels from ~/.zsh_history and ~/.bash_history
  service install|uninstall|status
                          start the manager automatically at login
  demo                    run with fictional sample data (nothing is touched)

Run `tunnel-manager COMMAND --help` for details.
"""


# ---------------------------------------------------------------- client side


class ApiError(RuntimeError):
    pass


def _api(port: int, method: str, path: str, body: Optional[dict] = None, timeout: float = 15) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"X-Tunnel-Manager": "1", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = str(e)
        raise ApiError(detail)
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(f"cannot reach the manager on port {port} ({getattr(e, 'reason', e)})")


def _ping(port: int) -> bool:
    try:
        return _api(port, "GET", "/api/ping", timeout=1).get("app") == "tunnel-manager"
    except ApiError:
        return False


def _server_port(wait: float = 8.0) -> Optional[int]:
    """Port of the running manager, or None if there is none.

    The lock is held a little before the HTTP server is up (starting) and a
    little after it is down (shutting down), so wait while that settles."""
    from .store import running_instance

    deadline = time.time() + wait
    while True:
        info = running_instance()
        if not info:
            return None
        port = info.get("port") or DEFAULT_PORT
        if _ping(port):
            return port
        if time.time() > deadline:
            return None
        time.sleep(0.2)


def _ensure_server(port: int = DEFAULT_PORT) -> int:
    """Return the port of a running manager, starting one in the background if needed."""
    existing = _server_port()
    if existing:
        return existing
    log = open(paths.service_log(), "ab")
    # --wait-for-lock: if an old manager is still shutting down, take over once it is gone
    subprocess.Popen(
        [sys.executable, "-m", "tunnel_manager", "serve", "--no-browser", "--wait-for-lock", "--port", str(port)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
    )
    for _ in range(150):
        time.sleep(0.1)
        if _ping(port):
            print(f"started tunnel-manager in the background on http://127.0.0.1:{port}")
            return port
    sys.exit(f"could not start the manager; see {paths.service_log()}")


def _resolve(ref: str):
    from .store import Store

    t = Store().find(ref)
    if t is None:
        sys.exit(f"no tunnel matches {ref!r} (use the exact name, a unique prefix, or the id)")
    return t


# ---------------------------------------------------------------- commands


def cmd_serve(args) -> None:
    from .store import acquire_instance_lock

    _setup_logging(verbose=args.verbose)
    lock = acquire_instance_lock(args.port)
    if lock is None:
        if args.wait_for_lock:
            logging.info("another manager is running; waiting to take over")
            while lock is None:
                time.sleep(0.5)
                lock = acquire_instance_lock(args.port)
        else:
            port = _server_port()
            print(f"tunnel-manager is already running on http://127.0.0.1:{port}")
            if not args.no_browser and port:
                webbrowser.open(f"http://127.0.0.1:{port}/")
            return

    import uvicorn

    from . import history, service, sshconfig
    from .server import create_app
    from .store import Store
    from .supervisor import Supervisor

    store = Store()
    if store.state.history_imported_at is None:
        result = store.merge_imported(history.scan_default_history(remote_home=sshconfig.remote_home))
        logging.info("first run: imported %s tunnels from shell history", result["added"])
    sup = Supervisor(store)
    sup.restore()
    sup.start_loop()

    server: Optional[uvicorn.Server] = None
    stop_tunnels = {"value": False}

    def request_shutdown(stop: bool) -> None:
        logging.info("shutdown requested (stop tunnels: %s)", stop)
        stop_tunnels["value"] = stop
        if server:
            server.should_exit = True

    app = create_app(store, sup, port=args.port, on_shutdown=request_shutdown)
    # open browser tabs keep an event stream open; don't let them block shutdown
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False,
                            timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    url = f"http://127.0.0.1:{args.port}/"

    def announce() -> None:
        for _ in range(100):
            if server.started:
                hint = "Ctrl+C stops the manager and its tunnels" if interactive else "`tunnel-manager stop` to quit"
                print(f"Tunnel Manager {__version__} running on {url}  ({hint})", flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                return
            time.sleep(0.05)

    # Lifecycle: tunnels belong to the manager.
    # * Ctrl+C on a foreground manager, or `tunnel-manager stop`: stop tunnels.
    # * SIGTERM (service restart, upgrade, logout): keep them running; the next
    #   manager re-attaches them, so restarts don't drop connections.
    interactive = sys.stdin.isatty() and not service.running_as_service()
    threading.Thread(target=announce, daemon=True).start()
    # uvicorn re-raises SIGTERM after its graceful shutdown; turn that into a
    # normal exit so the cleanup below runs
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.run()
    except KeyboardInterrupt:
        if interactive:
            stop_tunnels["value"] = True
    finally:
        if stop_tunnels["value"]:
            print("\nstopping tunnels...", flush=True)
        left = sup.shutdown(stop_tunnels=stop_tunnels["value"])
        lock.close()
        if left:
            print(f"{left} tunnel(s) left running; the manager re-attaches them when it starts again.", flush=True)
    if not server.started:
        sys.exit(f"could not listen on 127.0.0.1:{args.port} (port busy?) - try --port")


def cmd_open(args) -> None:
    port = _ensure_server(getattr(args, "port", DEFAULT_PORT))
    webbrowser.open(f"http://127.0.0.1:{port}/")
    print(f"Tunnel Manager: http://127.0.0.1:{port}/")


def cmd_stop(args) -> None:
    port = _server_port()
    if not port:
        print("the manager is not running")
        return
    from . import service

    st = service.status()
    _api(port, "POST", "/api/shutdown", {"stop_tunnels": True})
    print("stopped the manager and all tunnels")
    if st.get("loaded"):
        print("note: the login service is installed; it starts the manager again at next login "
              "(`tunnel-manager service uninstall` to remove it)")


def _status_label(rt: dict) -> str:
    st = rt.get("status", "stopped")
    if st == "up" and rt.get("connected_at"):
        return f"up {_fmt_duration(time.time() - rt['connected_at'])}"
    if st == "retrying" and rt.get("next_retry_at"):
        return f"retry in {max(0, int(rt['next_retry_at'] - time.time()))}s"
    return st


def _fmt_duration(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def cmd_list(args) -> None:
    port = _server_port()
    if port:
        tunnels = _api(port, "GET", "/api/state")["tunnels"]
    else:
        from .store import Store

        tunnels = [dict(t.model_dump(), runtime={"status": "-"}) for t in Store().tunnels()]
    if args.json:
        print(json.dumps(tunnels, indent=2))
        return
    favs = sorted((t for t in tunnels if t["favorite"]), key=lambda t: t["order"])
    rest = sorted((t for t in tunnels if not t["favorite"]), key=lambda t: -t["last_used_at"])
    rows = []
    for t in favs + rest:
        fw = " ".join(f"-{f['type']} {_spec(f)}" for f in t["forwards"])
        if len(fw) > 60:
            fw = fw[:57] + "..."
        rows.append(("★" if t["favorite"] else " ", _status_label(t["runtime"]), t["name"],
                     ("♻" if t["keep_alive"] else " "), fw, t["id"]))
    if not rows:
        print("no tunnels yet - add one with `tunnel-manager add -L 8080:localhost:80 myhost`")
        return
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print(f"{r[0]} {r[1]:<{w[1]}}  {r[2]:<{w[2]}}  {r[3]} {r[4]:<{w[4]}}  {r[5]}")
    if not port:
        print("\n(manager not running - status unknown)")


def _spec(f: dict) -> str:
    from .models import Forward

    return Forward.model_validate(f).spec()


def cmd_add(argv: List[str]) -> None:
    """add [--fav] [--name NAME] [--no-keep-alive] [--start] [--open-url URL] [ssh] SSH-ARGS..."""
    from .models import Tunnel
    from .sshcmd import parse_ssh_command

    opts = {"favorite": False, "name": None, "keep_alive": True, "start": False, "open_url": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(
                "usage: tunnel-manager add [--fav] [--name NAME] [--no-keep-alive] [--start] [--open-url URL] SSH-ARGS\n\n"
                "Adds a tunnel to the history (or to favourites with --fav). SSH-ARGS are regular ssh\n"
                "arguments, with or without a leading `ssh`, or one quoted command line:\n\n"
                "  tunnel-manager add -L 8080:localhost:8080 myhost\n"
                "  tunnel-manager add --fav --name notebook -- ssh -N -L 8888:localhost:8888 gpu-box\n"
                "  tunnel-manager add --fav \"ssh -R 3129:127.0.0.1:3128 build-server\"\n"
                "  tunnel-manager add -L 8090:/srv/app/app.sock -J bastion alice@10.0.0.7\n"
            )
            return
        if a == "--fav":
            opts["favorite"] = True
        elif a == "--no-keep-alive":
            opts["keep_alive"] = False
        elif a == "--start":
            opts["start"] = True
        elif a in ("--name", "--open-url") and i + 1 < len(argv):
            opts[a[2:].replace("-", "_")] = argv[i + 1]
            i += 1
        elif a == "--":
            i += 1
            break
        else:
            break
        i += 1
    rest = argv[i:]
    if not rest:
        sys.exit("add: missing ssh arguments, e.g. `tunnel-manager add -L 8080:localhost:80 myhost`")
    cmdline = rest[0] if len(rest) == 1 and " " in rest[0] else shlex.join(rest)
    if not cmdline.lstrip().startswith(("ssh ", "autossh ")):
        cmdline = "ssh " + cmdline
    parsed = parse_ssh_command(cmdline)
    if not parsed or not parsed.forwards:
        sys.exit("add: no -L / -R / -D forward found in: " + cmdline)
    for s in parsed.skipped:
        print(f"warning: skipped {s}", file=sys.stderr)
    body = {
        "host": parsed.host, "user": parsed.user, "port": parsed.port, "identity_file": parsed.identity_file,
        "jump": parsed.jump, "forwards": [f.model_dump() for f in parsed.forwards],
        "name": opts["name"], "favorite": opts["favorite"], "keep_alive": opts["keep_alive"],
        "open_url": opts["open_url"],
    }
    port = _server_port()
    if opts["start"] and not port:
        port = _ensure_server()
    if port:
        body["start"] = opts["start"]
        try:
            res = _api(port, "POST", "/api/tunnels", body)
        except ApiError as e:
            sys.exit(f"add: {e}")
        name, created = res["name"], res["created"]
    else:
        from pydantic import ValidationError

        from .store import Store

        try:
            t = Tunnel.model_validate(dict(body, source="manual"))
        except ValidationError as e:
            sys.exit(f"add: {e}")
        t, created = Store().add(t)
        name = t.name
    where = "favourites" if opts["favorite"] else "history"
    if created:
        print(f"added “{name}” to {where}" + (" and started it" if opts["start"] else ""))
    else:
        print(f"“{name}” already exists" + (" - added to favourites" if opts["favorite"] else ""))


def cmd_up(args) -> None:
    t = _resolve(args.ref)
    port = _ensure_server()
    _api(port, "POST", f"/api/tunnels/{t.id}/start")
    for _ in range(60):
        time.sleep(0.25)
        rt = next(x for x in _api(port, "GET", "/api/state")["tunnels"] if x["id"] == t.id)["runtime"]
        if rt["status"] in ("up", "failed", "retrying"):
            break
    print(f"{t.name}: {_status_label(rt)}")
    if rt.get("last_error") and rt["status"] != "up":
        print(f"  {rt['last_error']['message']}\n  hint: {rt['last_error']['hint']}")


def cmd_down(args) -> None:
    port = _server_port()
    if args.all:
        if port:
            _api(port, "POST", "/api/stop-all")
            print("stopped all tunnels")
        else:
            _kill_leftovers()
        return
    if not args.ref:
        sys.exit("down: give a tunnel name/id or --all")
    t = _resolve(args.ref)
    if not port:
        sys.exit("the manager is not running (nothing to stop)")
    _api(port, "POST", f"/api/tunnels/{t.id}/stop")
    print(f"stopped {t.name}")


def _kill_leftovers() -> None:
    """Without a manager: stop ssh processes recorded in runtime.json."""
    from .supervisor import Supervisor

    from .store import Store

    store = Store()
    sup = Supervisor.__new__(Supervisor)
    n = 0
    for tid, rec in Supervisor._load_runtime(sup).items():
        if store.get(tid):
            store.touch(tid, desired="stopped")
        proc = Supervisor._verify(rec)
        if proc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            n += 1
    try:
        paths.runtime_file().unlink()
    except OSError:
        pass
    print(f"stopped {n} leftover tunnel(s)")


def _set_fav(ref: str, fav: bool) -> None:
    t = _resolve(ref)
    port = _server_port()
    if port:
        _api(port, "PUT", f"/api/tunnels/{t.id}", {"favorite": fav})
    else:
        from .store import Store

        Store().update(t.id, {"favorite": fav})
    print(f"{t.name}: {'added to' if fav else 'removed from'} favourites")


def cmd_rm(args) -> None:
    t = _resolve(args.ref)
    if not args.yes:
        answer = input(f"Delete tunnel “{t.name}” ({len(t.forwards)} forward(s) to {t.host})? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return
    port = _server_port()
    if port:
        _api(port, "DELETE", f"/api/tunnels/{t.id}")
    else:
        from .store import Store

        Store().delete(t.id)
    print(f"deleted {t.name}")


def cmd_logs(args) -> None:
    t = _resolve(args.ref)
    p = paths.tunnel_log(t.id)
    if not p.exists():
        print("(no log yet)")
        return
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 8000))
        sys.stdout.write(f.read().decode("utf-8", errors="replace"))
        sys.stdout.flush()
        while args.follow:
            chunk = f.read()
            if chunk:
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
            else:
                time.sleep(0.5)


def cmd_import(args) -> None:
    port = _server_port()
    if port:
        res = _api(port, "POST", "/api/import-history", timeout=60)
    else:
        from . import history, sshconfig
        from .store import Store

        cands = history.scan_default_history(remote_home=sshconfig.remote_home)
        res = Store().merge_imported(cands)
        res["found"] = len(cands)
    print(f"found {res['found']} tunnels in shell history: {res['added']} new, "
          f"{res['updated']} refreshed, {res['ignored']} previously deleted (skipped)")


def cmd_service(args) -> None:
    from . import service

    if args.action == "install":
        res = service.install(port=args.port)
        print(f"installed {res['path']}\nThe manager now starts at login on http://127.0.0.1:{args.port}/")
        if _server_port() and not service.running_as_service():
            print("A foreground manager is running; the service takes over (and keeps your tunnels) once it exits.")
    elif args.action == "uninstall":
        res = service.uninstall()
        print("removed the login item" if res.get("removed") else "no login item was installed")
    else:
        st = service.status()
        print(json.dumps(st, indent=2))


def cmd_demo(args) -> None:
    from .demo import run_demo

    run_demo(port=args.port, open_browser=not args.no_browser)


# ---------------------------------------------------------------- plumbing


def _setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(paths.manager_log(), maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tunnel-manager", usage=USAGE, add_help=False)
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--version", action="version", version=f"tunnel-manager {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the manager in the foreground")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--no-browser", action="store_true", help="don't open the web UI")
    s.add_argument("--wait-for-lock", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("-v", "--verbose", action="store_true")

    s = sub.add_parser("open")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("stop")
    s = sub.add_parser("list")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("up")
    s.add_argument("ref", metavar="NAME|ID")
    s = sub.add_parser("down")
    s.add_argument("ref", nargs="?", metavar="NAME|ID")
    s.add_argument("--all", action="store_true")
    for name in ("fav", "unfav"):
        s = sub.add_parser(name)
        s.add_argument("ref", metavar="NAME|ID")
    s = sub.add_parser("rm")
    s.add_argument("ref", metavar="NAME|ID")
    s.add_argument("-y", "--yes", action="store_true")
    s = sub.add_parser("logs")
    s.add_argument("ref", metavar="NAME|ID")
    s.add_argument("-f", "--follow", action="store_true")
    sub.add_parser("import-history")
    s = sub.add_parser("service")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s = sub.add_parser("demo")
    s.add_argument("--port", type=int, default=5767)
    s.add_argument("--no-browser", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "add":  # free-form ssh arguments: parsed by hand
        return cmd_add(argv[1:])
    p = _parser()
    args = p.parse_args(argv)
    if args.help and not args.cmd:
        print(USAGE)
        return
    handlers = {
        None: cmd_open,
        "serve": cmd_serve,
        "open": cmd_open,
        "stop": cmd_stop,
        "list": cmd_list,
        "up": cmd_up,
        "down": cmd_down,
        "fav": lambda a: _set_fav(a.ref, True),
        "unfav": lambda a: _set_fav(a.ref, False),
        "rm": cmd_rm,
        "logs": cmd_logs,
        "import-history": cmd_import,
        "service": cmd_service,
        "demo": cmd_demo,
    }
    try:
        handlers[args.cmd](args)
    except ApiError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        pass
