"""Authenticated loopback HTTP API and composition root."""
from __future__ import annotations

import json
import threading
import re
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import VERSION
from .ledger import Ledger
from .model import (MAX_INGRESS_TEXT_CHARS, MAX_JSON_BYTES, LibraryError, bearer_ok,
                    canonical_url, now, require_object, safe_error)
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
        payload = require_object(payload, canonical=True)
        entries, created_spools = [], []
        try:
            if set(payload) - {"text", "title", "source_label", "files", "urls", "idempotency_key"}: raise LibraryError("unsupported submit field")
            label = payload.get("source_label")
            if label is not None and (not isinstance(label, str) or len(label) > 160): raise LibraryError("invalid source label")
            if payload.get("title") is not None and (not isinstance(payload["title"], str) or len(payload["title"]) > 300): raise LibraryError("invalid title")
            if not isinstance(payload.get("urls", []), list) or not isinstance(payload.get("files", []), list): raise LibraryError("urls and files must be arrays")
            text = payload.get("text")
            if text is not None:
                if not isinstance(text, str) or len(text) > MAX_INGRESS_TEXT_CHARS:
                    raise LibraryError(f"text must be at most {MAX_INGRESS_TEXT_CHARS} characters")
                for url in extract_urls(text): canonical_url(url); entries.append({"kind": "url", "locator": url, "metadata": {"source_label": label}})
                remaining = text_without_urls(text)
                if remaining:
                    spool_path = self.ledger.spool(remaining); created_spools.append(spool_path)
                    entries.append({"kind": "text", "spool_path": spool_path, "metadata": {"title": payload.get("title"), "source_label": label}})
            for url in payload.get("urls", []):
                if not isinstance(url, str): raise LibraryError("URL must be a string")
                canonical_url(url)
                entries.append({"kind": "url", "locator": url, "metadata": {"source_label": label}})
            for row in payload.get("files", []):
                if not isinstance(row, dict) or set(row) != {"path"} or not isinstance(row.get("path"), str): raise LibraryError("file path is required")
                entries.append({"kind": "file", "locator": row["path"], "metadata": {"source_label": label}})
            if not entries: raise LibraryError("at least one URL, text, or file is required")
            if len(entries) > 500: raise LibraryError("batch has more than 500 items")
            result = self.ledger.create_batch(entries, payload.get("idempotency_key"))
        except Exception:
            self.ledger.discard_unreferenced_spools(created_spools)
            raise
        self.ledger.discard_unreferenced_spools(created_spools)
        return result

    def resume(self, item_id: str, payload: dict) -> dict:
        payload = require_object(payload, canonical=True)
        if set(payload) - {"text", "title", "file_path", "ocr", "canonical_url", "publish_date", "author", "fetched_at", "provenance"}:
            raise LibraryError("unsupported resume field")
        if "text" in payload and (not isinstance(payload["text"], str) or len(payload["text"]) > MAX_INGRESS_TEXT_CHARS):
            raise LibraryError("invalid resume text")
        item = self.ledger.get_item(item_id)
        if item["status"] == "confirmed": return self._backfill_confirmed(item, payload)
        if item["status"] == "duplicate" or item.get("document_id") or (item["metadata"].get("upload_intent") and not item.get("task_uuid")):
            raise LibraryError("item cannot be resumed", "resume_conflict", 409)
        title = payload.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > 300): raise LibraryError("invalid resume title")
        ocr = payload.get("ocr", [])
        if not isinstance(ocr, list) or len(ocr) > 32 or any(not isinstance(row, dict) or set(row) - {"image_url", "text", "method"} or any(not isinstance(value, str) or len(value) > 20_000 for value in row.values()) for row in ocr):
            raise LibraryError("invalid OCR provenance")
        if "text" in payload and "file_path" in payload: raise LibraryError("resume accepts text or file_path")
        citation = payload.get("canonical_url")
        if citation is not None:
            from .sources import platform_for, stable_id
            if item["kind"] != "url" or platform_for(item["private_locator"]) != "wechat" or not isinstance(citation, str):
                raise LibraryError("canonical supplement requires a WeChat URL")
            stable_id("wechat", citation)
            citation = canonical_url(citation)
        author, published, fetched, provenance = (payload.get(k) for k in ("author", "publish_date", "fetched_at", "provenance"))
        if author is not None and (not isinstance(author, str) or len(author) > 300): raise LibraryError("invalid author")
        if published is not None:
            try:
                if not isinstance(published, str) or date.fromisoformat(published).isoformat() != published: raise ValueError()
            except ValueError: raise LibraryError("invalid publish_date") from None
        if fetched is not None:
            try:
                if not isinstance(fetched, str) or len(fetched) > 40 or datetime.fromisoformat(fetched.replace("Z", "+00:00")).tzinfo is None: raise ValueError()
            except ValueError: raise LibraryError("invalid fetched_at") from None
        if any(v is not None for v in (citation, author, published, fetched)) and (not isinstance(provenance, str) or provenance not in {"operator-public", "public-html"}):
            raise LibraryError("verified metadata provenance is required")
        if "file_path" in payload:
            from .sources import checked_file
            checked_file(payload["file_path"], self.accepted_roots)
            if item["kind"] == "url" and ocr and citation is None and not item["metadata"].get("verified_canonical_url"):
                raise LibraryError("URL binary OCR supplement requires verified canonical identity", "identity_pending", 422)
        safe_ocr = [{key: row[key] for key in ("image_url", "method") if key in row} for row in ocr]
        created_spools = []
        try:
            ocr_spool = self.ledger.spool(json.dumps(ocr, ensure_ascii=False)) if ocr else None
            if ocr_spool: created_spools.append(ocr_spool)
            base_metadata = {**item["metadata"], "ocr": safe_ocr}
            base_metadata.update({k: v for k, v in {"verified_canonical_url": citation, "author": author,
                "published_at": published, "fetched_at": fetched, "metadata_provenance": provenance}.items() if v is not None})
            if ocr_spool: base_metadata["ocr_spool_path"] = ocr_spool
            replacement = None
            if "text" in payload:
                text_spool = self.ledger.spool(payload["text"]); created_spools.append(text_spool)
                replacement = {"spool_path": text_spool, "metadata": {**base_metadata, **({"title": title} if title is not None else {})}}
            elif "file_path" in payload:
                replacement = {"metadata": {**base_metadata, "supplement_file_path": payload["file_path"], **({"title": title} if title is not None else {})}}
            elif ocr or title is not None or citation is not None or author is not None or published is not None or fetched is not None:
                replacement = {"metadata": {**base_metadata, **({"title": title} if title is not None else {})}}
            self.ledger.resume(item_id, replacement)
        except Exception:
            for value in created_spools:
                path = Path(value)
                if path.parent == self.ledger.spool_dir and path.is_file() and not path.is_symlink(): path.unlink()
            raise
        return self.ledger.public_item(item_id)

    def _backfill_confirmed(self, item: dict, payload: dict) -> dict:
        """Operator metadata revision; never requeue, upload, or edit document content."""
        fields = {"canonical_url", "publish_date", "author", "fetched_at"}
        if (not set(payload).intersection(fields) or set(payload) - fields - {"provenance"} or
                payload.get("provenance") != "operator-public"):
            raise LibraryError("confirmed item accepts verified public metadata only", "resume_conflict", 409)
        if type(item.get("document_id")) is not int or item["document_id"] <= 0 or item.get("kind") != "url":
            raise LibraryError("confirmed source document is unavailable", "resume_conflict", 409)
        if not isinstance(item.get("private_locator"), str):
            raise LibraryError("confirmed source identity is unavailable", "identity_pending", 422)
        from .sources import platform_for, stable_id
        platform = platform_for(item["private_locator"])
        stable = str(item.get("source_id") or "").split(":revision:", 1)[0]
        if platform not in {"wechat", "xhs"} or not stable.startswith(platform + ":") or not item.get("content_hash"):
            raise LibraryError("confirmed source identity is unavailable", "identity_pending", 422)
        changes = {}
        if "canonical_url" in payload:
            candidate = payload["canonical_url"]
            if not isinstance(candidate, str): raise LibraryError("invalid canonical URL")
            if stable_id(platform, candidate) != stable: raise LibraryError("canonical identity mismatch", "identity_pending", 422)
            changes["lib_canonical_url"] = canonical_url(candidate)
        if "author" in payload:
            value = payload["author"]
            if not isinstance(value, str) or not value.strip() or len(value) > 300:
                raise LibraryError("invalid author")
            changes["lib_author"] = value.strip()
        if "publish_date" in payload:
            value = payload["publish_date"]
            try:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value: raise ValueError()
            except ValueError: raise LibraryError("invalid publish_date") from None
            changes["lib_publish_date"] = value
        if "fetched_at" in payload:
            value = payload["fetched_at"]
            try:
                if not isinstance(value, str) or len(value) > 40 or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None: raise ValueError()
            except ValueError: raise LibraryError("invalid fetched_at") from None
            changes["lib_fetched_at"] = value
        native = self.worker.native
        if not hasattr(native, "backfill_metadata"):
            raise LibraryError("native metadata backfill is unavailable", "native_failed", 409)
        changed = native.backfill_metadata(item["document_id"], item, changes, "operator-public")
        new_canonical = changes.get("lib_canonical_url", item.get("canonical_url"))
        if changed or new_canonical != item.get("canonical_url"):
            metadata = {**item["metadata"], "metadata_backfill_at": now(), "metadata_provenance": "operator-public"}
            self.ledger.transition(item["id"], "confirmed", "metadata_backfill", canonical_url=new_canonical,
                                   metadata=metadata, document_id=item["document_id"])
        return self.ledger.public_item(item["id"])

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
        server_version = "PaperlessLibrary/0.3.0"
        def log_message(self, fmt, *args): pass
        def _send(self, code, body):
            blob = json.dumps(body, ensure_ascii=False).encode(); self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(blob))); self.end_headers(); self.wfile.write(blob)
        def _body(self, *, canonical=False):
            try: size = int(self.headers.get("Content-Length", "0"))
            except ValueError: raise LibraryError("invalid content length") from None
            if size < 0 or size > MAX_JSON_BYTES: raise LibraryError("JSON request exceeds limit")
            try: return require_object(json.loads(self.rfile.read(size) or b"{}"), canonical=canonical)
            except json.JSONDecodeError: raise LibraryError("invalid JSON")
        def _auth(self):
            if not bearer_ok(self.headers.get("Authorization"), service_token): raise LibraryError("authentication required", "unauthorized", 401)
        def _dispatch(self):
            parsed, path = urlsplit(self.path), urlsplit(self.path).path
            parts = path.strip("/").split("/")
            if path == "/health" and self.command == "GET": return {"version": VERSION}
            self._auth()
            if path != "/" + "/".join(parts): raise LibraryError("route not found", "not_found", 404)
            if path == "/v1/batches" and self.command == "POST": return service.submit(self._body(canonical=True))
            if len(parts) == 4 and parts[:2] == ["v1", "batches"] and parts[3] == "cancel" and self.command == "POST": return service.ledger.cancel(parts[2])
            if len(parts) == 3 and parts[:2] == ["v1", "batches"] and self.command == "GET": return service.ledger.batch(parts[2])
            if len(parts) == 4 and parts[:2] == ["v1", "items"] and parts[3] == "resume" and self.command == "POST": return service.resume(parts[2], self._body(canonical=True))
            if len(parts) == 4 and parts[:2] == ["v1", "items"] and parts[3] == "reanalyze" and self.command == "POST": return service.reanalyze(parts[2], self._body())
            if path == "/v1/search" and self.command == "POST":
                body = self._body(); limit = body.get("limit", 8)
                if set(body) - {"query", "limit"}: raise LibraryError("unsupported search field")
                if type(limit) is not int or not 1 <= limit <= 8: raise LibraryError("limit must be 1-8")
                return service.searcher.search(body.get("query"), limit)
            if len(parts) == 4 and parts[:2] == ["v1", "documents"] and parts[3] in {"evidence", "state"}:
                if not re.fullmatch(r"[1-9][0-9]*", parts[2]): raise LibraryError("invalid document_id")
                document_id = int(parts[2])
                if parts[3] == "evidence" and self.command == "GET":
                    query = parse_qs(parsed.query).get("query", [""])[0]; return service.searcher.evidence(document_id, query)
                if parts[3] == "state" and self.command == "PATCH": return service.state(document_id, self._body())
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
