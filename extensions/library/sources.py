"""Source identity, guarded fetching and local-file extraction."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import re
import socket
import ssl
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from .model import LibraryError, canonical_url, normalized_text, now, sha256_bytes

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
ALLOWED = {"mp.weixin.qq.com", "weixin.qq.com", "xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com"}
DENY_FILE_WORDS = (".env", "credential", "secret", "profile", "token", "key")


def extract_urls(text: str) -> list[str]:
    return [match.rstrip(".,;:!?)]}") for match in URL_RE.findall(text)]


def text_without_urls(text: str) -> str:
    return URL_RE.sub("", text).strip()


def platform_for(url: str) -> str:
    host = urlsplit(url).hostname or ""
    if host.endswith("weixin.qq.com"): return "wechat"
    if host in {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com"}: return "xhs"
    raise LibraryError("source domain is not supported", "blocked_source", 422)


def stable_id(platform: str, url: str, *, text: str | None = None, file_bytes: bytes | None = None) -> str:
    if platform == "text":
        if text is None: raise LibraryError("text identity needs content")
        return "text:" + sha256_bytes(normalized_text(text).encode())
    if platform == "file":
        if file_bytes is None: raise LibraryError("file identity needs bytes")
        return "file:" + sha256_bytes(file_bytes)
    parts, query = urlsplit(url), parse_qs(urlsplit(url).query)
    if platform == "wechat":
        biz, mid, idx, sn = (query.get("__biz", [""])[0], query.get("mid", [""])[0],
                             query.get("idx", [""])[0], query.get("sn", [""])[0])
        if not (biz and mid and idx): raise LibraryError("WeChat URL needs fetched canonical identity", "identity_pending", 422)
        # sn is preferred yet identity remains safely scoped when absent.
        return "wechat:" + biz + ":" + mid + ":" + idx + (":" + sn if sn else "")
    if platform == "xhs":
        match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9_-]+)(?:/|$)", parts.path)
        if not match: raise LibraryError("XHS short link needs resolved canonical identity", "identity_pending", 422)
        return "xhs:" + match.group(1)
    raise LibraryError("unsupported platform")


def _global(ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    return address.is_global and not (address.is_private or address.is_loopback or address.is_link_local or
                                      address.is_reserved or address.is_multicast or address.is_unspecified)


class URLGuard:
    """Validates every hop and returns vetted addresses for a pinned connection."""
    def __init__(self, resolver: Callable[[str, int], list[str]] | None = None):
        self.resolver = resolver or self._resolve

    @staticmethod
    def _resolve(host: str, port: int) -> list[str]:
        return sorted({row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})

    def validate(self, url: str) -> tuple[str, int, list[str]]:
        if "\\" in url: raise LibraryError("unsafe source URL", "ssrf_blocked", 422)
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            raise LibraryError("unsafe source URL", "ssrf_blocked", 422)
        if p.port not in (None, 80, 443) or (p.port and p.port != (443 if p.scheme == "https" else 80)):
            raise LibraryError("unsafe source URL", "ssrf_blocked", 422)
        host = p.hostname.lower().rstrip(".")
        if host not in ALLOWED: raise LibraryError("source domain is not allowed", "ssrf_blocked", 422)
        addresses = self.resolver(host, p.port or (443 if p.scheme == "https" else 80))
        if not addresses or any(not _global(ip) for ip in addresses):
            raise LibraryError("source resolution is unsafe", "ssrf_blocked", 422)
        return host, p.port or (443 if p.scheme == "https" else 80), addresses


class SafeFetcher:
    """Small HTTP client whose injected transport must use the vetted IP list.

    The default transport deliberately requires a caller-provided implementation
    in production startup.  This avoids validate-then-reresolve mistakes and
    keeps library import free of ambient network side effects.
    """
    def __init__(self, guard: URLGuard, transport: Callable[[str, str, int, list[str]], tuple[int, dict, bytes]]):
        self.guard, self.transport = guard, transport

    def get(self, url: str, max_bytes: int = 4 * 1024 * 1024) -> tuple[str, dict, bytes]:
        current = url
        for _ in range(6):
            host, port, ips = self.guard.validate(current)
            status, headers, body = self.transport(current, host, port, ips)
            if len(body) > max_bytes: raise LibraryError("source response exceeds limit", "source_too_large", 422)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location") or headers.get("Location")
                if not location: raise LibraryError("source redirect missing location", "source_failed", 422)
                current = urljoin(current, location); continue
            return current, headers, body
        raise LibraryError("too many source redirects", "ssrf_blocked", 422)


def pinned_transport(url: str, host: str, port: int, ips: list[str]) -> tuple[int, dict, bytes]:
    """Connect to a validated address while preserving Host and TLS SNI.

    Callers receive the vetted addresses from ``URLGuard`` and this function
    uses one directly; it never asks DNS to resolve the host a second time.
    """
    parsed, ip = urlsplit(url), ips[0]
    target = parsed.path or "/"
    if parsed.query: target += "?" + parsed.query
    host_header = host + ((":" + str(port)) if port not in (80, 443) else "")
    try:
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            raw = socket.create_connection((ip, port), timeout=15)
            sock = context.wrap_socket(raw, server_hostname=host)
            conn = http.client.HTTPSConnection(host, port, timeout=15, context=context)
            conn.sock = sock
        else:
            conn = http.client.HTTPConnection(ip, port, timeout=15)
        conn.request("GET", target, headers={"Host": host_header, "User-Agent": "PaperlessLibrary/0.2.0", "Accept": "text/html,application/pdf,text/plain"})
        response = conn.getresponse(); body = response.read(4 * 1024 * 1024 + 1)
        return response.status, dict(response.getheaders()), body
    except OSError as exc:
        raise LibraryError("source connection failed", "source_failed", 502) from None
    finally:
        try: conn.close()
        except Exception: pass


class _WeChatParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.title = ""; self.author = ""; self.date = ""; self.body = []; self._tag = ""; self._body_stack = []; self._outside_stack = []; self.outside = []
    _VOID = {"br", "img", "meta", "link", "input", "hr", "source", "area", "base", "embed", "wbr"}
    @staticmethod
    def _hidden(tag, data, parent=False):
        style = str(data.get("style") or "").replace(" ", "").lower()
        return parent or tag in {"script", "style", "template", "noscript"} or str(data.get("aria-hidden") or "").lower() == "true" or "display:none" in style or "visibility:hidden" in style or "hidden" in data
    def handle_starttag(self, tag, attrs):
        data = dict(attrs); ident = data.get("id", "")
        if tag == "meta" and data.get("property") == "og:title": self.title = data.get("content", "")
        if ident in {"js_content", "img-content"} and not self._body_stack:
            if tag not in self._VOID: self._body_stack = [(tag, self._hidden(tag, data))]
        elif self._body_stack and tag not in self._VOID:
            self._body_stack.append((tag, self._hidden(tag, data, self._body_stack[-1][1])))
        elif tag not in self._VOID:
            self._outside_stack.append((tag, self._hidden(tag, data, self._outside_stack[-1][1] if self._outside_stack else False)))
        self._tag = ident or data.get("class", "")
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)
    def handle_endtag(self, tag):
        if self._body_stack:
            positions = [index for index, entry in enumerate(self._body_stack) if entry[0] == tag]
            if positions: del self._body_stack[positions[-1]:]
        else:
            positions = [index for index, entry in enumerate(self._outside_stack) if entry[0] == tag]
            if positions: del self._outside_stack[positions[-1]:]
    def handle_data(self, data):
        clean = normalized_text(data)
        if not clean: return
        if self._body_stack:
            if not self._body_stack[-1][1]: self.body.append(clean)
        elif not (self._outside_stack and self._outside_stack[-1][1]): self.outside.append(clean)
        if self._tag in {"js_name", "profile_nickname"}: self.author = clean
        if self._tag in {"publish_time", "js_publish_time"}: self.date = clean


def parse_wechat_html(html: str) -> dict:
    parser = _WeChatParser(); parser.feed(html); body = "\n".join(parser.body)
    # A real article wins even if prose mentions login/verification.
    if body:
        return {"status": "complete", "title": parser.title or "WeChat article", "author": parser.author or None,
                "published_at": parser.date or None, "body": body, "fetched_at": now()}
    outside = " ".join(parser.outside).lower()
    if any(x in outside for x in ("请在微信客户端打开", "请先登录", "登录后查看", "安全验证", "访问验证")):
        return {"status": "needs_login", "reason": "WeChat requires an authorized session"}
    if any(x in outside for x in ("已被发布者删除", "文章已删除", "此内容已被删除")):
        return {"status": "failed", "reason": "WeChat article is unavailable"}
    if not body: return {"status": "partial", "reason": "response is not an article"}


def checked_file(path_value: str, roots: list[str | Path]) -> Path:
    path = Path(path_value).expanduser()
    if path.is_symlink() or not path.is_file(): raise LibraryError("file must be a regular non-symlink", "file_blocked", 422)
    resolved = path.resolve()
    root_paths = [Path(root).expanduser().resolve() for root in roots]
    if not any(resolved.is_relative_to(root) for root in root_paths): raise LibraryError("file is outside accepted roots", "file_blocked", 422)
    if any(word in part.lower() for part in resolved.parts for word in DENY_FILE_WORDS): raise LibraryError("sensitive file path is not accepted", "file_blocked", 422)
    if resolved.suffix.lower() not in {".txt", ".md", ".pdf", ".docx"}: raise LibraryError("file type is not supported", "file_blocked", 422)
    return resolved


def extract_file(path: Path) -> tuple[bytes, str | None, str]:
    raw, suffix = path.read_bytes(), path.suffix.lower()
    if suffix in {".txt", ".md"}: return raw, raw.decode("utf-8", errors="replace"), "complete"
    if suffix == ".pdf":
        try:
            import fitz  # provided by parent runtime; not required merely to import this module
            text = "\n".join(page.get_text() for page in fitz.open(stream=raw, filetype="pdf"))
        except Exception:
            text = ""
        return raw, text or None, "needs_ocr" if not normalized_text(text) else "complete"
    if suffix == ".docx":
        import zipfile
        from xml.etree import ElementTree
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        text = "".join(node.text or "" for node in ElementTree.fromstring(xml).iter() if node.tag.endswith("}t"))
        return raw, text or None, "complete" if normalized_text(text) else "needs_ocr"
    raise AssertionError("checked_file controls suffix")
