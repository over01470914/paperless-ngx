"""Authenticated loopback HTTP API and composition root."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import VERSION
from .ledger import Ledger
from .model import LibraryError, bearer_ok, canonical_url, require_object, safe_error
from .search import LibrarySearch
from .sources import extract_urls, text_without_urls


class LibraryService:
    def __init__(self, ledger: Ledger, worker, search: LibrarySearch, writer, field_ids: dict[str, int], accepted_roots=None, legacy_migrator=None):
        self.ledger, self.worker, self.searcher, self.writer, self.field_ids = ledger, worker, search, writer, field_ids
        self.accepted_roots, self.legacy_migrator = accepted_roots or [], legacy_migrator
        self._stop, self._thread = None, None

    def start_worker(self) -> None:
        if self._thread and self._thread.is_alive(): return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self.worker.idle_loop, args=(self._stop,), daemon=True, name="paperless-library-worker")
        self._thread.start()

    def stop_worker(self) -> None:
        if self._stop: self._stop.set()
        if self._thread: self._thread.join(timeout=2)

    def submit(self, payload: dict) -> dict:
        entries, created_spools = [], []
        try:
            text = payload.get("text")
            if text is not None:
                if not isinstance(text, str) or len(text) > 200_000: raise LibraryError("text must be at most 200000 characters")
                for url in extract_urls(text): canonical_url(url); entries.append({"kind": "url", "locator": url})
                remaining = text_without_urls(text)
                if remaining:
                    spool_path = self.ledger.spool(remaining); created_spools.append(spool_path)
                    entries.append({"kind": "text", "spool_path": spool_path, "metadata": {"title": payload.get("title")}})
            for url in payload.get("urls", []):
                if not isinstance(url, str): raise LibraryError("URL must be a string")
                canonical_url(url)
                entries.append({"kind": "url", "locator": url})
            for row in payload.get("files", []):
                if not isinstance(row, dict) or not isinstance(row.get("path"), str): raise LibraryError("file path is required")
                entries.append({"kind": "file", "locator": row["path"]})
            if not entries: raise LibraryError("at least one URL, text, or file is required")
            if len(entries) > 500: raise LibraryError("batch has more than 500 items")
            result = self.ledger.create_batch(entries, payload.get("idempotency_key"))
        except Exception:
            self.ledger.discard_unreferenced_spools(created_spools)
            raise
        self.ledger.discard_unreferenced_spools(created_spools)
        return result

    def resume(self, item_id: str, payload: dict) -> dict:
        item = self.ledger.get_item(item_id)
        if item["status"] in {"confirmed", "duplicate"} or (item["metadata"].get("upload_intent") and not item.get("task_uuid")):
            raise LibraryError("item cannot be resumed", "resume_conflict", 409)
        title = payload.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > 300): raise LibraryError("invalid resume title")
        ocr = payload.get("ocr", [])
        if not isinstance(ocr, list) or len(ocr) > 32 or any(not isinstance(row, dict) or set(row) - {"image_url", "text", "method"} or any(not isinstance(value, str) or len(value) > 20_000 for value in row.values()) for row in ocr):
            raise LibraryError("invalid OCR provenance")
        if "text" in payload and "file_path" in payload: raise LibraryError("resume accepts text or file_path")
        if "text" in payload and (not isinstance(payload["text"], str) or len(payload["text"]) > 200_000): raise LibraryError("invalid resume text")
        if "file_path" in payload:
            from .sources import checked_file
            checked_file(payload["file_path"], self.accepted_roots)
            if item["kind"] == "url" and ocr:
                raise LibraryError("OCR with URL file supplement is not supported", "ocr_unsupported", 409)
        safe_ocr = [{key: row[key] for key in ("image_url", "method") if key in row} for row in ocr]
        created_spools = []
        try:
            ocr_spool = self.ledger.spool(json.dumps(ocr, ensure_ascii=False)) if ocr else None
            if ocr_spool: created_spools.append(ocr_spool)
            base_metadata = {**item["metadata"], "ocr": safe_ocr}
            if ocr_spool: base_metadata["ocr_spool_path"] = ocr_spool
            replacement = None
            if "text" in payload:
                text_spool = self.ledger.spool(payload["text"]); created_spools.append(text_spool)
                replacement = {"spool_path": text_spool, "metadata": {**base_metadata, **({"title": title} if title is not None else {})}}
            elif "file_path" in payload:
                replacement = {"metadata": {**base_metadata, "supplement_file_path": payload["file_path"], **({"title": title} if title is not None else {})}}
            elif ocr or title is not None:
                replacement = {"metadata": {**base_metadata, **({"title": title} if title is not None else {})}}
            self.ledger.resume(item_id, replacement)
        except Exception:
            for value in created_spools:
                path = Path(value)
                if path.parent == self.ledger.spool_dir and path.is_file() and not path.is_symlink(): path.unlink()
            raise
        return self.ledger.public_item(item_id)

    def state(self, document_id: int, payload: dict) -> dict:
        if any(key not in {"read", "starred", "pending"} or not isinstance(value, bool) for key, value in payload.items()): raise LibraryError("state fields must be booleans")
        return self.writer.update_state(document_id, self.field_ids, payload)

    def migrate(self, payload: dict) -> dict:
        if not self.legacy_migrator: raise LibraryError("legacy migration is not configured", "migration_unavailable", 409)
        return self.legacy_migrator(payload.get("limit"))

    def reanalyze(self, item_id: str, payload: dict) -> dict:
        version = payload.get("analysis_version")
        if not isinstance(version, str) or not version.strip() or len(version) > 80: raise LibraryError("approved analysis version is required")
        item = self.ledger.enqueue_reanalysis(item_id, version)
        return self.ledger.public_item(item["id"])


def handler_factory(service: LibraryService, service_token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperlessLibrary/0.2.0"
        def log_message(self, fmt, *args): pass
        def _send(self, code, body):
            blob = json.dumps(body, ensure_ascii=False).encode(); self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(blob))); self.end_headers(); self.wfile.write(blob)
        def _body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if size > 128 * 1024: raise LibraryError("JSON request exceeds limit")
            try: return require_object(json.loads(self.rfile.read(size) or b"{}"))
            except json.JSONDecodeError: raise LibraryError("invalid JSON")
        def _auth(self):
            if not bearer_ok(self.headers.get("Authorization"), service_token): raise LibraryError("authentication required", "unauthorized", 401)
        def _dispatch(self):
            parsed, path = urlsplit(self.path), urlsplit(self.path).path
            if path == "/health" and self.command == "GET": return {"version": VERSION}
            self._auth()
            if path == "/v1/batches" and self.command == "POST": return service.submit(self._body())
            if path.startswith("/v1/batches/") and path.endswith("/cancel") and self.command == "POST": return service.ledger.cancel(path.split("/")[3])
            if path.startswith("/v1/batches/") and self.command == "GET": return service.ledger.batch(path.split("/")[3])
            if path.startswith("/v1/items/") and path.endswith("/resume") and self.command == "POST": return service.resume(path.split("/")[3], self._body())
            if path.startswith("/v1/items/") and path.endswith("/reanalyze") and self.command == "POST": return service.reanalyze(path.split("/")[3], self._body())
            if path == "/v1/search" and self.command == "POST":
                body = self._body(); return service.searcher.search(body.get("query"), body.get("limit", 8))
            if path.startswith("/v1/documents/") and path.endswith("/evidence") and self.command == "GET":
                query = parse_qs(parsed.query).get("query", [""])[0]; return service.searcher.evidence(int(path.split("/")[3]), query)
            if path.startswith("/v1/documents/") and path.endswith("/state") and self.command == "PATCH": return service.state(int(path.split("/")[3]), self._body())
            if path == "/v1/migrate" and self.command == "POST": return service.migrate(self._body())
            raise LibraryError("route not found", "not_found", 404)
        def do_GET(self): self._run()
        def do_POST(self): self._run()
        def do_PATCH(self): self._run()
        def _run(self):
            try: self._send(200, self._dispatch())
            except LibraryError as exc: self._send(exc.status, {"error": exc.code, "message": safe_error(exc)})
            except Exception: self._send(500, {"error": "internal_error", "message": "internal service error"})
    return Handler


def serve(service: LibraryService, token: str):
    service.start_worker()
    return ThreadingHTTPServer(("127.0.0.1", 4388), handler_factory(service, token))
