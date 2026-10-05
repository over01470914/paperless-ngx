"""Stdio FastMCP facade for the authenticated local Library API."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

from mcp.server.fastmcp import FastMCP

from .model import LibraryError


class LocalAPI:
    def __init__(self, token: str, base: str = "http://127.0.0.1:4388"):
        if base != "http://127.0.0.1:4388": raise LibraryError("MCP API must use fixed local service")
        self.token, self.base = token, base
    def call(self, method: str, path: str, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base + path, data=data, method=method,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with build_opener().open(request, timeout=20) as response: return json.loads(response.read(512 * 1024))
        except HTTPError as exc:
            # No upstream response body or service token becomes MCP output.
            raise LibraryError("local Library API request failed", "local_api_failed", exc.code) from None


def api_from_runtime(config_path: str | None = None) -> LocalAPI:
    path = Path(config_path or os.environ.get("PAPERLESS_LIBRARY_CONFIG", "~/.config/paperless-ngx/library.json")).expanduser()
    data = json.loads(path.read_text(encoding="utf-8"))
    token = data.get("service_token")
    if not isinstance(token, str) or not token: raise LibraryError("runtime service token is unavailable")
    return LocalAPI(token)


def create_server(api: LocalAPI | None = None) -> FastMCP:
    client = api or api_from_runtime()
    server = FastMCP("paperless-library", instructions="Use only authenticated local Paperless Library API.")
    @server.tool()
    def submit_batch(text: str | None = None, urls: list[str] | None = None, files: list[dict] | None = None, title: str | None = None, idempotency_key: str | None = None) -> dict:
        return client.call("POST", "/v1/batches", {"text": text, "urls": urls or [], "files": files or [], "title": title, "idempotency_key": idempotency_key})
    @server.tool()
    def batch_status(batch_id: str) -> dict: return client.call("GET", "/v1/batches/" + quote(batch_id, safe=""))
    @server.tool()
    def resume_item(item_id: str, text: str | None = None, title: str | None = None, file_path: str | None = None, ocr: list[dict] | None = None) -> dict:
        payload = {key: value for key, value in {"text": text, "title": title, "file_path": file_path, "ocr": ocr}.items() if value is not None}
        return client.call("POST", "/v1/items/" + quote(item_id, safe="") + "/resume", payload)
    @server.tool()
    def cancel_batch(batch_id: str) -> dict: return client.call("POST", "/v1/batches/" + quote(batch_id, safe="") + "/cancel", {})
    @server.tool()
    def search_library(query: str, limit: int = 8) -> dict: return client.call("POST", "/v1/search", {"query": query, "limit": limit})
    @server.tool()
    def read_evidence(document_id: int, query: str) -> dict: return client.call("GET", "/v1/documents/" + str(int(document_id)) + "/evidence?" + urlencode({"query": query}))
    @server.tool()
    def set_reading_state(document_id: int, read: bool | None = None, starred: bool | None = None, pending: bool | None = None) -> dict:
        return client.call("PATCH", "/v1/documents/" + str(int(document_id)) + "/state", {k: v for k, v in {"read": read, "starred": starred, "pending": pending}.items() if v is not None})
    @server.tool()
    def migrate_legacy(limit: int | None = None) -> dict: return client.call("POST", "/v1/migrate", {} if limit is None else {"limit": limit})
    return server


if __name__ == "__main__":
    create_server().run(transport="stdio")
