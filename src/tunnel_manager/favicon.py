"""Fetch and cache the favicon of the web app behind a -L tunnel.

The page is loaded through the tunnel's local port only: redirects or icon
links that point anywhere else are ignored, responses are size-limited and
must actually be images.
"""

from __future__ import annotations

import base64
import http.client
import logging
import ssl
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit

from . import paths
from .models import Tunnel

log = logging.getLogger(__name__)

MAX_PAGE = 512 * 1024
MAX_ICON = 256 * 1024
TIMEOUT = 4.0
RETRY_FAILED_AFTER = 10 * 60
REFRESH_AFTER = 24 * 3600
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

EXT_TYPES = {
    "png": "image/png", "ico": "image/x-icon", "svg": "image/svg+xml", "gif": "image/gif",
    "jpg": "image/jpeg", "webp": "image/webp",
}


def sniff(data: bytes) -> Optional[str]:
    """File extension for image bytes, or None if it isn't an image we accept."""
    head = data[:512].lstrip()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] in (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"):
        return "ico"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048]):
        return "svg"
    return None


class _IconLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[Tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rel, href = a.get("rel", "").lower().split(), a.get("href", "").strip()
        if not href:
            return
        if "icon" in rel:
            # prefer svg / larger png icons over tiny ones
            rank = 0 if href.endswith(".svg") or "svg" in a.get("type", "") else 1
            self.links.append((rank, href))
        elif "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
            self.links.append((2, href))


class FaviconCache:
    def __init__(self, on_update: Callable[[], None] = lambda: None):
        self.dir = paths.home() / "favicons"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.on_update = on_update
        self._lock = threading.Lock()
        self._inflight: Set[str] = set()
        self._attempted: Dict[str, float] = {}

    # ------------------------------------------------------------- lookup

    def path(self, tunnel_id: str) -> Optional[Path]:
        for ext in EXT_TYPES:
            p = self.dir / f"{tunnel_id}.{ext}"
            if p.exists():
                return p
        return None

    def version(self, tunnel_id: str) -> Optional[int]:
        p = self.path(tunnel_id)
        try:
            return int(p.stat().st_mtime) if p else None
        except OSError:
            return None

    def forget(self, tunnel_id: str) -> None:
        for ext in EXT_TYPES:
            (self.dir / f"{tunnel_id}.{ext}").unlink(missing_ok=True)
        with self._lock:
            self._attempted.pop(tunnel_id, None)

    # ------------------------------------------------------------- fetching

    def wants(self, t: Tunnel) -> bool:
        """Whether a (re)fetch for this up tunnel is due."""
        if not page_url(t):
            return False
        now = time.time()
        with self._lock:
            if t.id in self._inflight:
                return False
            last = self._attempted.get(t.id, 0)
        v = self.version(t.id)
        if v is not None:
            return now - max(v, last) > REFRESH_AFTER
        return now - last > RETRY_FAILED_AFTER

    def fetch_async(self, t: Tunnel, submit: Callable) -> None:
        with self._lock:
            if t.id in self._inflight:
                return
            self._inflight.add(t.id)
            self._attempted[t.id] = time.time()
        submit(self._fetch_and_store, t)

    def _fetch_and_store(self, t: Tunnel) -> None:
        try:
            got = fetch_favicon(page_url(t) or "")
            if got:
                data, ext = got
                for other in EXT_TYPES:
                    if other != ext:
                        (self.dir / f"{t.id}.{other}").unlink(missing_ok=True)
                (self.dir / f"{t.id}.{ext}").write_bytes(data)
                log.info("favicon for %s: %s bytes (%s)", t.name, len(data), ext)
                self.on_update()
        except Exception:
            log.debug("favicon fetch failed for %s", t.name, exc_info=True)
        finally:
            with self._lock:
                self._inflight.discard(t.id)


def page_url(t: Tunnel) -> Optional[str]:
    """The local web page of a tunnel (http/https on localhost), if any."""
    url = t.default_open_url()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in LOCAL_HOSTS:
        return None
    return url


def _get(url: str, limit: int) -> Tuple[int, Dict[str, str], bytes]:
    parts = urlsplit(url)
    host, port = parts.hostname or "127.0.0.1", parts.port
    if parts.scheme == "https":
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # localhost through our own tunnel
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request("GET", path, headers={"User-Agent": "tunnel-manager favicon fetcher", "Accept": "*/*"})
        resp = conn.getresponse()
        body = resp.read(limit + 1)
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, body
    finally:
        conn.close()


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return pa.scheme == pb.scheme and pa.hostname == pb.hostname and pa.port == pb.port


def _get_following(url: str, limit: int, origin: str) -> Optional[Tuple[str, Dict[str, str], bytes]]:
    for _ in range(4):
        status, headers, body = _get(url, limit)
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            nxt = urljoin(url, headers["location"])
            if not _same_origin(nxt, origin):
                return None
            url = nxt
            continue
        if len(body) > limit:
            return None
        return url, headers, body
    return None


def _decode_data_uri(uri: str) -> Optional[bytes]:
    header, _, payload = uri.partition(",")
    try:
        if header.endswith(";base64"):
            return base64.b64decode(payload, validate=False)
        from urllib.parse import unquote_to_bytes
        return unquote_to_bytes(payload)
    except ValueError:
        return None


def fetch_favicon(page: str) -> Optional[Tuple[bytes, str]]:
    """(bytes, extension) of the page's favicon, or None."""
    candidates: List[str] = []
    try:
        got = _get_following(page, MAX_PAGE, page)
    except OSError:
        got = None
    if got:
        final_url, headers, body = got
        if "html" in headers.get("content-type", "html"):
            parser = _IconLinks()
            try:
                parser.feed(body.decode("utf-8", errors="replace"))
            except Exception:
                pass
            for _, href in sorted(parser.links):
                if href.startswith("data:"):
                    data = _decode_data_uri(href)
                    if data and len(data) <= MAX_ICON and sniff(data):
                        return data, sniff(data)  # type: ignore[return-value]
                    continue
                candidates.append(urljoin(final_url, href))
    candidates.append(urljoin(page, "/favicon.ico"))
    for url in candidates:
        if not _same_origin(url, page):
            continue
        try:
            got = _get_following(url, MAX_ICON, page)
        except OSError:
            continue
        if not got:
            continue
        _, headers, data = got
        ext = sniff(data)
        if ext and data:
            return data, ext
    return None
