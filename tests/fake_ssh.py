"""A stand-in for ssh used by the tests.

Behaviour is read from the file named in $FAKE_SSH_MODE_FILE so a test can
change it between attempts:

  ok           listen on every -L/-D port, report success, run until killed
  auth         fail like a refused key
  drop:<secs>  behave like `ok`, then lose the connection after <secs>
  hang         never authenticate
"""

import os
import signal
import socket
import sys
import time


def main() -> None:
    mode = "ok"
    path = os.environ.get("FAKE_SSH_MODE_FILE")
    if path and os.path.exists(path):
        mode = open(path).read().strip() or "ok"
    args = sys.argv[1:]
    host = args[-1]

    if mode == "auth":
        print(f"user@{host}: Permission denied (publickey).", file=sys.stderr, flush=True)
        sys.exit(255)
    if mode == "hang":
        time.sleep(3600)
        return

    socks = []
    for i, a in enumerate(args):
        if a in ("-L", "-D"):
            port = int(args[i + 1].split(":")[0] if args[i + 1].split(":")[0].isdigit() else args[i + 1].split(":")[1])
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                print(f"bind [127.0.0.1]:{port}: Address already in use", file=sys.stderr, flush=True)
                sys.exit(255)
            s.listen()
            socks.append(s)

    def bye(*_):
        print("Transferred: sent 1234, received 5678 bytes, in 1.0 seconds", file=sys.stderr, flush=True)
        sys.exit(255)

    signal.signal(signal.SIGTERM, bye)
    print(f'Authenticated to {host} ([127.0.0.1]:22) using "publickey".', file=sys.stderr, flush=True)
    if mode.startswith("drop:"):
        time.sleep(float(mode.split(":")[1]))
        print(f"Connection to {host} closed by remote host.", file=sys.stderr, flush=True)
        sys.exit(255)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
