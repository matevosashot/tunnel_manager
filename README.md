# Tunnel Manager

A local web UI for your SSH port forwards (`-L`, `-R`, `-D`, including remote unix sockets):
turn tunnels on and off, keep them alive through network loss and sleep, pin favourites,
and reuse everything you ever ran from your shell history.

> 🚧 Work in progress — full documentation and screenshots are coming.

## Install

```bash
pipx install git+https://github.com/matevosashot/tunnel_manager.git
# or: uv tool install git+https://github.com/matevosashot/tunnel_manager.git
# or, inside a virtualenv: pip install git+https://github.com/matevosashot/tunnel_manager.git
```

## Run

```bash
tunnel-manager            # starts the manager in the background and opens http://127.0.0.1:5766
tunnel-manager --help     # all commands (add, list, up, down, fav, logs, service, ...)
```

Only key-based SSH authentication is used; password logins are always disabled.
State, history and logs live in `~/.cache/tunnel-manager/`.

## License

MIT
