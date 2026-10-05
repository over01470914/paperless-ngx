#!/usr/bin/env python3
"""Fixed-upstream companion proxy. Browser auth remains native Paperless auth."""

from __future__ import annotations

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from radar import validate_upstream

STATIC = Path(__file__).resolve().parent / "static"
FILES = {
    "/radar/": ("index.html", "text/html; charset=utf-8"),
    "/radar/index.html": ("index.html", "text/html; charset=utf-8"),
    "/radar/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/radar/style.css": ("style.css", "text/css; charset=utf-8"),
}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection",
       "forwarded"}
MAX_REQUEST = 16 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024


def safe_target(target: str) -> str:
    parts = urlsplit(target)
    decoded = parts.path
    for _ in range(10):
        further = unquote(decoded)
        if further == decoded:
            break
        decoded = further
    if (parts.scheme or parts.netloc or not parts.path.startswith("/") or
            decoded.startswith("//") or unquote(decoded) != decoded or
            "\\" in decoded or "\x00" in decoded or
            any(segment in {".", ".."} for segment in decoded.split("/"))):
        raise ValueError("unsafe request target")
    return parts.path + ("?" + parts.query if parts.query else "")


def filtered_headers(headers, *, request=False) -> list[tuple[str, str]]:
    nominated = set()
    for value in headers.get_all("Connection", []):
        nominated.update(x.strip().lower() for x in value.split(","))
    excluded = HOP | nominated
    if request:
        excluded |= {"host", "content-length", "accept-encoding"}
    else:
        excluded |= {"content-length"}
    return [(key, value) for key, value in headers.items()
            if key.lower() not in excluded and
            (not request or not key.lower().startswith("x-forwarded-"))]


def request_length(headers, method: str) -> int:
    if headers.get("Transfer-Encoding"):
        raise ValueError("chunked request bodies are unsupported")
    lengths = headers.get_all("Content-Length", [])
    if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
        raise ValueError("invalid request Content-Length")
    if method in {"POST", "PUT", "PATCH"} and not lengths:
        raise ValueError("Content-Length required")
    size = int(lengths[0]) if lengths else 0
    if size > MAX_REQUEST:
        raise OverflowError("request body exceeds limit")
    return size


def rewrite_origin(value: str, incoming_host: str, upstream_origin: str) -> str:
    incoming_origin = "http://" + incoming_host
    if incoming_host and (value == incoming_origin or value.startswith(incoming_origin + "/")):
        return upstream_origin + value[len(incoming_origin):]
    return value


def make_handler(upstream: str):
    parsed = urlsplit(validate_upstream(upstream))
    upstream_host = f"{parsed.hostname}:{parsed.port}"
    upstream_origin = f"http://{upstream_host}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "ArticleRadar/0.1.0"

        def do_GET(self): self._handle()
        def do_HEAD(self): self._handle()
        def do_POST(self): self._handle()
        def do_PUT(self): self._handle()
        def do_PATCH(self): self._handle()
        def do_DELETE(self): self._handle()
        def do_OPTIONS(self): self._handle()

        def _handle(self):
            try:
                target = safe_target(self.path)
            except ValueError:
                self.send_error(400, "Unsafe path")
                return
            path = urlsplit(target).path
            if path == "/radar":
                self.send_response(308)
                self.send_header("Location", "/radar/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/radar/"):
                self._static(path)
                return
            self._proxy(target)

        def _static(self, path):
            if self.command not in {"GET", "HEAD"} or path not in FILES:
                self.send_error(404)
                return
            filename, content_type = FILES[path]
            data = (STATIC / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'none'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _proxy(self, target):
            try:
                length = request_length(self.headers, self.command)
            except OverflowError:
                self.send_error(413, "Request body exceeds limit")
                return
            except ValueError as exc:
                self.send_error(411 if "required" in str(exc) else 400, str(exc))
                return
            body = self.rfile.read(length) if length else None
            headers = dict(filtered_headers(self.headers, request=True))
            headers["Host"] = upstream_host
            headers["Accept-Encoding"] = "identity"
            for key in ("Origin", "Referer"):
                value = headers.get(key)
                if value:
                    headers[key] = rewrite_origin(value, self.headers.get("Host", ""), upstream_origin)
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=20)
            try:
                connection.request(self.command, target, body=body, headers=headers)
                response = connection.getresponse()
                if response.length is not None and response.length > MAX_RESPONSE:
                    raise ValueError("upstream response exceeds limit")
                data = response.read(MAX_RESPONSE + 1)
                if len(data) > MAX_RESPONSE:
                    raise ValueError("upstream response exceeds limit")
                self.send_response_only(response.status, response.reason)
                for key, value in filtered_headers(response.headers):
                    if key.lower() == "location" and value.startswith(upstream_origin + "/"):
                        value = value[len(upstream_origin):]
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
            except (OSError, http.client.HTTPException, ValueError):
                self.send_error(502, "Fixed upstream unavailable or response too large")
            finally:
                connection.close()

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Article Radar fixed-upstream proxy")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=4387)
    parser.add_argument("--upstream", default="http://127.0.0.1:4386")
    args = parser.parse_args()
    handler = make_handler(args.upstream)
    ThreadingHTTPServer((args.host, args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
