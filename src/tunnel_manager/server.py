"""HTTP API + static UI."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__, history, paths, service, sshconfig
from .models import Tunnel
from .sshcmd import display_command, tilde_warning
from .store import Store
from .supervisor import Supervisor, port_free, port_owner

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
EDITABLE = {"name", "host", "user", "port", "identity_file", "jump", "forwards", "keep_alive", "favorite", "open_url"}
CONNECTION_FIELDS = {"host", "user", "port", "identity_file", "jump", "forwards"}


class Broadcaster:
    """Wakes every open SSE stream when state changes (thread-safe)."""

    def __init__(self) -> None:
        self._subs: Set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def subscribe(self) -> asyncio.Queue:
        self._loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def poke() -> None:
            for q in list(self._subs):
                if q.empty():
                    q.put_nowait(True)

        try:
            loop.call_soon_threadsafe(poke)
        except RuntimeError:
            pass


def _validation_message(e: ValidationError) -> str:
    msgs = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "__root__")
        msg = err.get("msg", "").replace("Value error, ", "")
        msgs.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(msgs)


def create_app(
    store: Store,
    supervisor: Supervisor,
    port: int = 5766,
    extra_hosts: Optional[List[str]] = None,
    on_shutdown: Optional[Callable[[bool], None]] = None,
    demo: bool = False,
) -> FastAPI:
    app = FastAPI(title="Tunnel Manager", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    bus = Broadcaster()
    store.on_change(bus.publish)
    supervisor.on_change(bus.publish)
    pool = ThreadPoolExecutor(max_workers=8)

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"} | set(extra_hosts or [])
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # DNS-rebinding protection: only answer to our own host names
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        # CSRF protection: a custom header can't be sent cross-origin without
        # a CORS preflight, which we never approve
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if request.headers.get("x-tunnel-manager") != "1" or (origin and origin not in allowed_origins):
                return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    # ------------------------------------------------------------ helpers

    def tunnel_view(t: Tunnel, snap: Dict[str, dict]) -> dict:
        d = t.model_dump()
        d["runtime"] = snap.get(t.id) or {"status": "stopped"}
        d["command"] = display_command(t)
        d["open"] = t.default_open_url()
        d["warning"] = tilde_warning(t)
        return d

    def state() -> dict:
        snap = supervisor.snapshot()
        return {
            "version": __version__,
            "now": time.time(),
            "online": supervisor.online,
            "demo": demo,
            "home": _tilde(str(paths.home())),
            "settings": store.settings.model_dump(),
            "tunnels": [tunnel_view(t, snap) for t in store.tunnels()],
            "events": supervisor.recent_events(),
        }

    def get_or_404(tunnel_id: str) -> Tunnel:
        t = store.get(tunnel_id)
        if t is None:
            raise HTTPException(404, "no such tunnel")
        return t

    def build_tunnel(data: dict) -> Tunnel:
        try:
            return Tunnel.model_validate(data)
        except ValidationError as e:
            raise HTTPException(422, _validation_message(e))

    # ------------------------------------------------------------ state

    @app.get("/api/ping")
    def ping():
        return {"app": "tunnel-manager", "version": __version__, "pid": os.getpid()}

    @app.get("/api/state")
    def get_state():
        return state()

    @app.get("/api/events")
    async def events(request: Request):
        q = bus.subscribe()

        async def stream():
            try:
                yield f"data: {_json(state())}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    await asyncio.sleep(0.05)  # coalesce bursts
                    yield f"data: {_json(state())}\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ tunnels

    @app.post("/api/tunnels")
    def create_tunnel(body: dict = Body(...)):
        start = bool(body.pop("start", False))
        data = {k: v for k, v in body.items() if k in EDITABLE}
        data.setdefault("keep_alive", True)
        data["source"] = "manual"
        t, created = store.add(build_tunnel(data))
        if start:
            supervisor.start(t.id)
        return {"id": t.id, "created": created, "name": t.name}

    @app.put("/api/tunnels/{tunnel_id}")
    def update_tunnel(tunnel_id: str, body: dict = Body(...)):
        before = get_or_404(tunnel_id)
        changes = {k: v for k, v in body.items() if k in EDITABLE}
        try:
            after = store.update(tunnel_id, changes)
        except ValidationError as e:
            raise HTTPException(422, _validation_message(e))
        except ValueError as e:
            raise HTTPException(409, str(e))
        rt = supervisor.snapshot().get(tunnel_id, {})
        active = rt.get("status") in ("starting", "up", "retrying", "offline")
        conn_changed = any(
            getattr(before, f) != getattr(after, f) for f in CONNECTION_FIELDS
        )
        if active and conn_changed:
            pool.submit(supervisor.restart, tunnel_id)
        elif active and before.keep_alive and not after.keep_alive and rt.get("status") in ("retrying", "offline"):
            supervisor.stop(tunnel_id)
        return {"id": after.id, "name": after.name, "restarted": active and conn_changed}

    @app.delete("/api/tunnels/{tunnel_id}")
    def delete_tunnel(tunnel_id: str):
        get_or_404(tunnel_id)
        supervisor.forget(tunnel_id)
        store.delete(tunnel_id)
        return {"ok": True}

    @app.post("/api/tunnels/{tunnel_id}/start")
    def start_tunnel(tunnel_id: str):
        get_or_404(tunnel_id)
        supervisor.start(tunnel_id)
        return {"ok": True}

    @app.post("/api/tunnels/{tunnel_id}/stop")
    def stop_tunnel(tunnel_id: str):
        get_or_404(tunnel_id)
        supervisor.stop(tunnel_id)
        return {"ok": True}

    @app.post("/api/tunnels/{tunnel_id}/open")
    def open_tunnel(tunnel_id: str):
        url = get_or_404(tunnel_id).default_open_url()
        if not url:
            raise HTTPException(400, "this tunnel has nothing to open")
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise HTTPException(500, f"could not open {url}: {e}")
        return {"ok": True, "url": url}

    @app.get("/api/tunnels/{tunnel_id}/logs")
    def tunnel_logs(tunnel_id: str, offset: int = -1, limit: int = 64_000):
        get_or_404(tunnel_id)
        p = paths.tunnel_log(tunnel_id)
        try:
            size = p.stat().st_size
        except OSError:
            return {"offset": 0, "text": "", "path": _tilde(str(p))}
        start = max(0, size - limit) if offset < 0 or offset > size else offset
        with open(p, "rb") as f:
            f.seek(start)
            text = f.read(min(limit, size - start)).decode("utf-8", errors="replace")
        return {"offset": size, "text": text, "path": _tilde(str(p)), "truncated": start > 0 and offset < 0}

    @app.put("/api/favorites/order")
    def favorites_order(body: dict = Body(...)):
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPException(422, "ids must be a list of tunnel ids")
        store.reorder_favorites(ids)
        return {"ok": True}

    @app.post("/api/stop-all")
    def stop_all():
        supervisor.stop_all()
        return {"ok": True}

    @app.post("/api/preview")
    def preview(body: dict = Body(...)):
        """Validate a tunnel draft from the editor and report problems early."""
        editing = body.pop("id", None)
        data = {k: v for k, v in body.items() if k in EDITABLE}
        try:
            t = Tunnel.model_validate(data)
        except ValidationError as e:
            return {"ok": False, "error": _validation_message(e)}
        warnings = []
        tw = tilde_warning(t)
        if tw:
            warnings.append(tw)
        dup = store.find_duplicate(t, exclude_id=editing)
        if dup:
            warnings.append(f"Identical to the existing tunnel “{dup.name}” — saving will reuse it.")
        own_pid = supervisor.snapshot().get(editing or "", {}).get("pid")
        for bind, p in t.local_ports():
            if not port_free(bind, p):
                owner = port_owner(p)
                if own_pid and owner and f"pid {own_pid})" in owner:
                    continue
                warnings.append(f"Local port {p} is in use by {owner or 'another process'}.")
        for other in store.tunnels():
            if other.id == editing or other.id == (dup.id if dup else None):
                continue
            clash = {p for _, p in other.local_ports()} & {p for _, p in t.local_ports()}
            if clash:
                warnings.append(f"Port {', '.join(map(str, sorted(clash)))} is also used by “{other.name}”.")
                break
        return {"ok": True, "name": t.name, "command": display_command(t), "open": t.default_open_url(), "warnings": warnings}

    # ------------------------------------------------------------ ssh info

    @app.get("/api/hosts")
    def hosts():
        aliases = sshconfig.list_hosts()
        resolved = list(pool.map(sshconfig.resolve, aliases))
        out = []
        for alias, r in zip(aliases, resolved):
            out.append({
                "alias": alias,
                "hostname": r.get("hostname"),
                "user": r.get("user"),
                "port": r.get("port"),
                "proxyjump": r.get("proxyjump"),
                "error": r.get("error"),
            })
        return out

    @app.get("/api/resolve")
    def resolve(host: str, user: Optional[str] = None, port: Optional[int] = None):
        from .models import is_valid_host
        if not is_valid_host(host):
            raise HTTPException(422, "invalid host")
        return sshconfig.resolve(host, user or None, port)

    @app.get("/api/keys")
    def keys():
        return sshconfig.list_keys()

    # ------------------------------------------------------------ history, settings, service

    @app.post("/api/import-history")
    def import_history():
        cands = history.scan_default_history(remote_home=sshconfig.remote_home)
        result = store.merge_imported(cands)
        result["found"] = len(cands)
        return result

    @app.get("/api/settings")
    def get_settings():
        return store.settings.model_dump()

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        try:
            return store.update_settings(body).model_dump()
        except ValidationError as e:
            raise HTTPException(422, _validation_message(e))

    @app.get("/api/service")
    def service_status():
        return service.status()

    @app.post("/api/service/install")
    def service_install():
        try:
            result = service.install(port=port)
        except Exception as e:
            raise HTTPException(500, str(e))
        # hand over to the background service: it is waiting for our lock and
        # will adopt the running tunnels
        if not service.running_as_service() and on_shutdown:
            threading.Timer(0.8, on_shutdown, args=(False,)).start()
            result["handover"] = True
        return result

    @app.post("/api/shutdown")
    def shutdown(body: dict = Body(default={})):
        if on_shutdown is None:
            raise HTTPException(400, "not supported")
        threading.Timer(0.3, on_shutdown, args=(bool(body.get("stop_tunnels", True)),)).start()
        return {"ok": True}

    @app.post("/api/service/uninstall")
    def service_uninstall():
        try:
            return service.uninstall()
        except Exception as e:
            raise HTTPException(500, str(e))

    if STATIC.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")

    return app


def _tilde(p: str) -> str:
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home + os.sep) else p


def _json(obj) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"))
