import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tunnel_manager.favicon import MAX_ICON, FaviconCache, fetch_favicon, page_url, sniff
from tunnel_manager.models import Forward, Tunnel

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
ICO = b"\x00\x00\x01\x00" + b"\1" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'

ROUTES = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        route = ROUTES.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>not found</html>")
            return
        status, headers, body = route
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def site():
    ROUTES.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def html(body: str):
    return (200, {"Content-Type": "text/html"}, body.encode())


def test_sniff():
    assert sniff(PNG) == "png" and sniff(ICO) == "ico" and sniff(SVG) == "svg"
    assert sniff(b"<html>") is None


def test_link_icon(site):
    ROUTES["/"] = html('<head><link rel="shortcut icon" href="/static/i.png"></head>')
    ROUTES["/static/i.png"] = (200, {"Content-Type": "image/png"}, PNG)
    assert fetch_favicon(site + "/") == (PNG, "png")


def test_svg_preferred_and_redirect_followed(site):
    ROUTES["/"] = (302, {"Location": "/login"}, b"")
    ROUTES["/login"] = html('<link rel="icon" sizes="16x16" href="small.png"><link rel="icon" href="/logo.svg">')
    ROUTES["/small.png"] = (200, {}, PNG)
    ROUTES["/logo.svg"] = (200, {}, SVG)
    assert fetch_favicon(site + "/") == (SVG, "svg")


def test_fallback_to_favicon_ico(site):
    ROUTES["/"] = html("<title>no icon here</title>")
    ROUTES["/favicon.ico"] = (200, {}, ICO)
    assert fetch_favicon(site + "/") == (ICO, "ico")


def test_data_uri(site):
    uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
    ROUTES["/"] = html(f'<link rel="icon" href="{uri}">')
    assert fetch_favicon(site + "/") == (PNG, "png")


def test_never_leaves_the_tunnel(site):
    ROUTES["/"] = html('<link rel="icon" href="http://example.com/evil.png">')
    assert fetch_favicon(site + "/") is None  # no /favicon.ico either
    ROUTES["/"] = (302, {"Location": "http://example.com/"}, b"")
    assert fetch_favicon(site + "/") is None


def test_rejects_big_or_non_image(site):
    ROUTES["/"] = html('<link rel="icon" href="/big.png">')
    ROUTES["/big.png"] = (200, {}, PNG + b"\0" * MAX_ICON)
    ROUTES["/favicon.ico"] = (200, {"Content-Type": "image/x-icon"}, b"<html>not an icon</html>")
    assert fetch_favicon(site + "/") is None


def test_page_url_only_for_local_web():
    def t(**kw):
        return Tunnel(host="gpu", forwards=[Forward(type="L", listen_port=8080, target_host="localhost", target_port=80)], **kw)
    assert page_url(t()) == "http://localhost:8080"
    assert page_url(t(open_url="https://localhost:8080/app")) == "https://localhost:8080/app"
    assert page_url(t(open_url="http://example.com/")) is None
    assert page_url(t(open_url="vnc://localhost:5901")) is None


def test_cache_store_and_forget(site, isolated_home):
    ROUTES["/"] = html('<link rel="icon" href="/i.png">')
    ROUTES["/i.png"] = (200, {}, PNG)
    port = int(site.rsplit(":", 1)[1])
    tun = Tunnel(host="gpu", forwards=[Forward(type="L", listen_port=port, target_host="localhost", target_port=80)])
    updates = []
    cache = FaviconCache(on_update=lambda: updates.append(1))
    assert cache.wants(tun) and cache.version(tun.id) is None
    cache.fetch_async(tun, lambda fn, arg: fn(arg))  # run inline
    assert updates and cache.path(tun.id).read_bytes() == PNG
    assert not cache.wants(tun)  # fresh
    cache.forget(tun.id)
    assert cache.path(tun.id) is None
