"""Narrow Paperless v10 HTTP clients and document readback helpers."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .enrich import extractive
from .model import LIB_FIELDS, LibraryError, canonical_url, normalized_text, now, safe_error, sha256_bytes
from .sources import platform_for, stable_id


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


class NativeClient:
    def __init__(self, base: str, token: str):
        parsed = urlsplit(base)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path not in ("", "/"):
            raise LibraryError("native URL must be fixed loopback HTTP")
        self.base, self.token, self.opener = base.rstrip("/"), token, build_opener(_NoRedirect())

    def request(self, method: str, path: str, payload: Any = None, content_type="application/json") -> Any:
        if not path.startswith("/api/") or ".." in path or "\\" in path: raise LibraryError("invalid native API path")
        data = None if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        headers = {"Authorization": "Token " + self.token, "Accept": "application/json; version=10"}
        if data is not None: headers["Content-Type"] = content_type
        try:
            with self.opener.open(Request(self.base + path, data=data, headers=headers, method=method), timeout=20) as response:
                blob = response.read(16 * 1024 * 1024 + 1)
                if len(blob) > 16 * 1024 * 1024: raise LibraryError("native response too large", "native_failed", 502)
                return json.loads(blob) if blob else {}
        except HTTPError as exc:
            if exc.code in (401, 403): raise LibraryError("native permission denied", "native_permission", 403) from None
            raise LibraryError("native API request failed", "native_failed", 502) from None

    def pages(self, path: str) -> list[dict]:
        result, visited = [], set()
        while path:
            if path in visited or len(visited) >= 1000: raise LibraryError("native pagination loop", "native_failed", 502)
            visited.add(path); data = self.request("GET", path)
            if isinstance(data, list): result.extend(data); break
            if not isinstance(data, dict) or not isinstance(data.get("results"), list): raise LibraryError("invalid native pagination", "native_failed", 502)
            result.extend(data["results"]); next_url = data.get("next")
            if next_url:
                p = urlsplit(next_url)
                if p.scheme or p.netloc:
                    if f"{p.scheme}://{p.netloc}" != self.base: raise LibraryError("native pagination escaped loopback", "native_failed", 502)
                    path = p.path + ("?" + p.query if p.query else "")
                else: path = next_url
            else: path = None
        return result

    def document(self, document_id: int) -> dict: return self.request("GET", f"/api/documents/{int(document_id)}/")
    def search(self, query: str) -> list[dict]:
        # Paperless v10 DRF full-text search is permission-aware.  ``query``
        # is not its search parameter and can silently return no results.
        return self.pages("/api/documents/?" + urlencode({"search": query, "page_size": 100}))

    def field_ids(self) -> dict[str, int]:
        fields = self.pages("/api/custom_fields/"); found = {row.get("name"): row for row in fields}
        ids = {}
        for name, kind in LIB_FIELDS.items():
            row = found.get(name)
            if not row: raise LibraryError("library schema is not bootstrapped", "schema_missing", 409)
            if row.get("data_type") != kind: raise LibraryError("library custom field type mismatch", "schema_mismatch", 409)
            ids[name] = int(row["id"])
        return ids

    def ensure_schema(self) -> dict[str, int]:
        """Writer-only bootstrap, with type readback before use."""
        fields = self.pages("/api/custom_fields/"); found = {row.get("name"): row for row in fields}
        for name, kind in LIB_FIELDS.items():
            row = found.get(name)
            if not row:
                row = self.request("POST", "/api/custom_fields/", {"name": name, "data_type": kind})
            if row.get("name") != name or row.get("data_type") != kind:
                raise LibraryError("library custom field type mismatch", "schema_mismatch", 409)
            found[name] = row
        return {name: int(found[name]["id"]) for name in LIB_FIELDS}

    def ensure_named_resource(self, endpoint: str, name: str) -> int:
        rows = [row for row in self.pages(endpoint) if row.get("name") == name]
        if len(rows) > 1: raise LibraryError("ambiguous native resource", "schema_mismatch", 409)
        row = rows[0] if rows else self.request("POST", endpoint, {"name": name})
        if row.get("name") != name or not isinstance(row.get("id"), int):
            raise LibraryError("native resource readback mismatch", "native_failed", 502)
        return row["id"]

    def update_state(self, document_id: int, field_ids: dict[str, int], state: dict) -> dict:
        document = self.document(document_id); values = custom_fields(document)
        changes = {"lib_reading_state": "read" if state.get("read") else "unread"} if "read" in state else {}
        for key in ("starred", "pending"):
            if key in state: changes["lib_" + key] = bool(state[key])
        values.update({field_ids[key]: value for key, value in changes.items()})
        payload = {"custom_fields": [{"field": key, "value": value} for key, value in values.items()]}
        self.request("PATCH", f"/api/documents/{int(document_id)}/", payload)
        updated = self.document(document_id); actual = custom_fields(updated)
        if any(actual.get(field_ids[key]) != value for key, value in changes.items()): raise LibraryError("native state readback mismatch", "native_failed", 502)
        return {"id": int(document_id), "read": actual.get(field_ids["lib_reading_state"]) == "read",
                "starred": bool(actual.get(field_ids["lib_starred"])), "pending": bool(actual.get(field_ids["lib_pending"]))}


def custom_fields(document: dict) -> dict[int, Any]:
    fields = {}
    for row in document.get("custom_fields") or []:
        if isinstance(row, dict) and isinstance(row.get("field"), int): fields[row["field"]] = row.get("value")
    return fields


def value_map(document: dict, ids: dict[str, int]) -> dict[str, Any]:
    fields = custom_fields(document); result = {name: fields.get(field_id) for name, field_id in ids.items()}
    # compatibility reads support visible legacy Radar documents.
    legacy = {row.get("field"): row.get("value") for row in document.get("custom_fields") or [] if isinstance(row, dict)}
    result.setdefault("wx_source_url", legacy.get("wx_source_url")); result.setdefault("wx_source_id", legacy.get("wx_source_id"))
    return result


def strip_analysis(content: str) -> str:
    return content.split("── 分析資料（非原文；未經事實查核）──", 1)[0]


def archive_text(source: str, ocr: str | None = None) -> str:
    value = "── SOURCE ──\n" + source.strip() + "\n── END SOURCE ──"
    if ocr: value += "\n── OCR ──\n" + ocr.strip() + "\n── END OCR ──"
    return value


class NativeIngestor:
    """Writer/reader split.  It never deletes and reconciles only readable docs."""
    def __init__(self, reader: NativeClient, writer: NativeClient, field_ids: dict[str, int], resources: dict[str, int] | None = None,
                 permission_ids: dict[str, int] | None = None, analyzer=None, legacy_field_ids: dict[str, int] | None = None):
        required = {"reader_user_id", "writer_user_id", "boss_user_id"}
        if permission_ids is not None and set(permission_ids) != required:
            raise LibraryError("native permission IDs are required", "config_invalid", 409)
        self.reader, self.writer, self.field_ids, self.resources = reader, writer, field_ids, resources or {}
        self.permission_ids, self.analyzer, self.legacy_field_ids = permission_ids, analyzer or extractive, legacy_field_ids or {}

    def find_existing(self, source_id, canonical, content_hash, original_url=None):
        def public_identity(url):
            if not isinstance(url, str) or not url:
                return None
            try:
                clean = canonical_url(url)
                return stable_id(platform_for(clean), clean)
            except (LibraryError, ValueError):
                return None

        caller_url_id = public_identity(canonical)
        original_id = public_identity(original_url)
        caller_ids = {value for value in (source_id, caller_url_id, original_id) if value}
        if len(caller_ids) > 1:
            return None
        caller_id = next(iter(caller_ids), None)
        for doc in self.reader.pages("/api/documents/?page_size=100"):
            fields = custom_fields(doc)
            known_id = fields.get(self.field_ids["lib_source_id"]) or fields.get(self.legacy_field_ids.get("wx_source_id"))
            known_url = fields.get(self.field_ids["lib_canonical_url"])
            legacy_url = fields.get(self.legacy_field_ids.get("wx_source_url"))
            document_ids = {value for value in (known_id, public_identity(known_url), public_identity(legacy_url)) if value}
            if len(document_ids) > 1 or (caller_id and document_ids and caller_id not in document_ids):
                continue
            document_id = next(iter(document_ids), None)
            if (caller_id and document_id == caller_id) or (
                legacy_url and original_url and legacy_url == original_url and
                (not caller_id or not document_id or caller_id == document_id)
            ) or (
                content_hash and not caller_id and not (original_url or canonical) and
                not document_id and not (known_url or legacy_url) and
                fields.get(self.field_ids["lib_content_hash"]) == content_hash
            ):
                return int(doc["id"])
        return None

    def source_changed(self, document_id: int, content_hash: str) -> bool:
        fields = custom_fields(self.reader.document(document_id))
        previous = fields.get(self.field_ids["lib_content_hash"])
        return isinstance(previous, str) and bool(previous) and previous != content_hash

    def find_version(self, version_id: str, content_hash: str) -> int | None:
        for doc in self.reader.pages("/api/documents/?page_size=100"):
            fields = custom_fields(doc)
            if fields.get(self.field_ids["lib_source_id"]) == version_id and fields.get(self.field_ids["lib_content_hash"]) == content_hash:
                return int(doc["id"])
        return None

    def upload(self, data, body, source_id, canonical, content_hash):
        boundary = "PaperlessLibrary" + uuid.uuid4().hex
        analysis = data.get("legacy_analysis") or self.analyzer(data.get("body", body))
        analysis_version = "legacy-preserved" if data.get("legacy_analysis") else analysis.get("version", "extractive-1")
        values = {"lib_platform": data["platform"], "lib_source_id": source_id,
                  "lib_original_url": data.get("original_url"), "lib_canonical_url": canonical,
                  "lib_author": data.get("author"), "lib_publish_date": data.get("published_at"),
                  "lib_fetched_at": data.get("fetched_at"), "lib_content_hash": content_hash,
                  "lib_completeness": data.get("completeness", "complete"), "lib_extraction_status": data.get("extraction_status", data.get("completeness", "complete")),
                  "lib_provenance": json.dumps({"source": data.get("provenance", {"adapter": data["platform"]}),
                                                "source_label": data.get("source_label"), "ocr": data.get("ocr_provenance", []),
                                                "ocr_indexed": bool(data.get("ocr_indexed", False)),
                                                "ai": {"method": analysis.get("method"), "version": analysis_version}}, ensure_ascii=False),
                  "lib_analysis": json.dumps(analysis, ensure_ascii=False),
                  "lib_analysis_version": analysis_version, "lib_reading_state": "unread", "lib_starred": False,
                  "lib_pending": False}
        parts = []
        def add(name, value, filename=None, mime=None):
            header = f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
            if filename: header += f"; filename=\"{filename}\""
            header += "\r\n" + (f"Content-Type: {mime}\r\n" if mime else "") + "\r\n"
            parts.append(header.encode() + (value if isinstance(value, bytes) else str(value).encode()) + b"\r\n")
        file_bytes = None if data.get("upload_text_with_ocr") else data.get("file_bytes")
        filename = "library.txt" if data.get("upload_text_with_ocr") else data.get("filename", "library.txt")
        mime = "text/plain; charset=utf-8" if data.get("upload_text_with_ocr") else data.get("mime", "text/plain; charset=utf-8")
        add("document", file_bytes if file_bytes is not None else body.encode(), filename, mime); add("title", data["title"])
        if "document_type" in self.resources: add("document_type", self.resources["document_type"])
        if "tag" in self.resources: add("tags", self.resources["tag"])
        add("custom_fields", json.dumps({str(self.field_ids[k]): v for k, v in values.items() if v is not None}, ensure_ascii=False))
        parts.append(f"--{boundary}--\r\n".encode())
        response = self.writer.request("POST", "/api/documents/post_document/", b"".join(parts), "multipart/form-data; boundary=" + boundary)
        task = response if isinstance(response, str) else (response.get("task_id") or response.get("task") if isinstance(response, dict) else None)
        return task if isinstance(task, str) else None

    def task_result(self, task_uuid):
        tasks = self.writer.pages("/api/tasks/?" + urlencode({"task_id": task_uuid}))
        rows = [row for row in tasks if row.get("task_id") == task_uuid]
        if len(rows) > 1: raise LibraryError("ambiguous native task", "native_failed", 502)
        if not rows: return None
        row, status = rows[0], str(rows[0].get("status") or "").lower()
        if status == "success": return {"document_id": (row.get("result_data") or {}).get("document_id")}
        if status in {"failure", "revoked"}: return {"duplicate": bool((row.get("result_data") or {}).get("duplicate_of")), "document_id": (row.get("result_data") or {}).get("duplicate_of")}
        return None

    def attach_ocr(self, document_id: int, source: str, ocr: str, provenance: list[dict]) -> None:
        """Append OCR to the reader's indexed content, leaving the original file alone."""
        text = ocr.strip()
        if not text or not isinstance(provenance, list):
            raise LibraryError("invalid OCR attachment", "ocr_unindexed", 409)
        before = self.reader.document(document_id)
        existing = before.get("content")
        if not isinstance(existing, str):
            raise LibraryError("native source content is unavailable", "ocr_unindexed", 409)
        start, end = "── OCR ──\n", "\n── END OCR ──"
        if "── OCR ──" in existing or "── END OCR ──" in existing:
            if existing.count(start) != 1 or existing.count(end) != 1 or existing.split(start, 1)[1].split(end, 1)[0] != text:
                raise LibraryError("native OCR section conflicts with attachment", "ocr_unindexed", 409)
            content = existing
        else:
            content = existing + ("\n" if existing and not existing.endswith("\n") else "") + start + text + end
        fields = custom_fields(before)
        raw = fields.get(self.field_ids["lib_provenance"])
        try: record = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError: raise LibraryError("native OCR provenance is invalid", "ocr_unindexed", 409) from None
        if not isinstance(record, dict): raise LibraryError("native OCR provenance is invalid", "ocr_unindexed", 409)
        ocr_origin = [{key: row[key] for key in ("image_url", "method") if key in row} for row in provenance if isinstance(row, dict)]
        if len(ocr_origin) != len(provenance): raise LibraryError("invalid OCR provenance", "ocr_unindexed", 409)
        record["ocr"] = ocr_origin
        fields[self.field_ids["lib_provenance"]] = json.dumps(record, ensure_ascii=False)
        if content != existing or custom_fields(before) != fields:
            self.writer.request("PATCH", f"/api/documents/{int(document_id)}/",
                                {"content": content, "custom_fields": [{"field": key, "value": value} for key, value in fields.items()]})
        after = self.reader.document(document_id)
        if after.get("content") != content or custom_fields(after) != fields:
            raise LibraryError("native OCR attachment readback mismatch", "ocr_unindexed", 502)

    def verify_indexed_ocr(self, document_id: int, ocr: str) -> bool:
        content = str(self.reader.document(document_id).get("content") or "")
        if content.count("── OCR ──\n") != 1 or content.count("\n── END OCR ──") != 1: return False
        section = content.split("── OCR ──\n", 1)[1].split("\n── END OCR ──", 1)[0]
        return bool(ocr.strip()) and section == ocr.strip()

    def mark_ocr_indexed(self, document_id: int, completeness: str) -> None:
        """Record success only after verify_indexed_ocr read the native document."""
        before = self.reader.document(document_id)
        fields = custom_fields(before)
        raw = fields.get(self.field_ids["lib_provenance"])
        try: record = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError: raise LibraryError("native OCR provenance is invalid", "native_failed", 409) from None
        if not isinstance(record, dict): raise LibraryError("native OCR provenance is invalid", "native_failed", 409)
        if completeness not in {"complete", "partial"}: raise LibraryError("invalid OCR completeness")
        if (record.get("ocr_indexed") is True and fields.get(self.field_ids["lib_completeness"]) == completeness and
                fields.get(self.field_ids["lib_extraction_status"]) == completeness): return
        record["ocr_indexed"] = True
        fields[self.field_ids["lib_provenance"]] = json.dumps(record, ensure_ascii=False)
        fields[self.field_ids["lib_completeness"]] = completeness
        fields[self.field_ids["lib_extraction_status"]] = completeness
        self.writer.request("PATCH", f"/api/documents/{int(document_id)}/",
                            {"custom_fields": [{"field": key, "value": value} for key, value in fields.items()]})
        after = self.reader.document(document_id)
        if custom_fields(after) != fields or after.get("content") != before.get("content"):
            raise LibraryError("native OCR provenance readback mismatch", "native_failed", 502)

    def backfill_metadata(self, document_id: int, item: dict, changes: dict[str, str], provenance: str) -> bool:
        """Patch only trusted metadata fields and read back the entire custom-field set."""
        allowed = {"lib_canonical_url", "lib_author", "lib_publish_date", "lib_fetched_at"}
        if not changes or set(changes) - allowed or provenance != "operator-public":
            raise LibraryError("invalid metadata backfill")
        before = self.reader.document(document_id)
        fields = custom_fields(before)
        source_id = fields.get(self.field_ids["lib_source_id"])
        content_hash = fields.get(self.field_ids["lib_content_hash"])
        if source_id != item.get("source_id") or content_hash != item.get("content_hash"):
            raise LibraryError("native source identity readback mismatch", "native_failed", 409)
        changed = {name: value for name, value in changes.items() if fields.get(self.field_ids[name]) != value}
        if not changed: return False
        raw = fields.get(self.field_ids["lib_provenance"])
        try: record = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError: raise LibraryError("native provenance is invalid", "native_failed", 409) from None
        if not isinstance(record, dict) or not isinstance(record.get("metadata_revisions", []), list):
            raise LibraryError("native provenance is invalid", "native_failed", 409)
        revisions = record.setdefault("metadata_revisions", [])
        if len(revisions) >= 100: raise LibraryError("metadata revision limit reached", "native_failed", 409)
        revisions.append({"revision": len(revisions) + 1, "at": now(), "provenance": provenance,
                          "fields": sorted(changed)})
        expected = dict(fields)
        for name, value in changed.items(): expected[self.field_ids[name]] = value
        expected[self.field_ids["lib_provenance"]] = json.dumps(record, ensure_ascii=False)
        self.writer.request("PATCH", f"/api/documents/{int(document_id)}/",
                            {"custom_fields": [{"field": key, "value": value} for key, value in expected.items()]})
        after = self.reader.document(document_id)
        if (custom_fields(after) != expected or after.get("content") != before.get("content") or
                after.get("title") != before.get("title")):
            raise LibraryError("native metadata readback mismatch", "native_failed", 502)
        return True

    def reanalyze(self, document_id: int, version: str) -> str:
        document = self.reader.document(document_id); fields = custom_fields(document)
        source = strip_analysis(str(document.get("content") or ""))
        source = source.replace("── SOURCE ──\n", "").replace("\n── END SOURCE ──", "")
        content_hash = fields.get(self.field_ids["lib_content_hash"])
        if not isinstance(content_hash, str) or not source: raise LibraryError("native source is unavailable for analysis", "analysis_failed", 409)
        source_hash = sha256_bytes(normalized_text(source).encode())
        existing = fields.get(self.field_ids["lib_analysis"])
        try: existing = json.loads(existing) if isinstance(existing, str) else {}
        except ValueError: existing = {}
        if fields.get(self.field_ids["lib_analysis_version"]) == version and existing.get("content_hash") == source_hash:
            return "unchanged"
        expected = getattr(self.analyzer, "version", "extractive-1")
        if version != expected: raise LibraryError("analysis version is not approved", "analysis_failed", 409)
        analysis = self.analyzer(source)
        values = custom_fields(document)
        values[self.field_ids["lib_analysis"]] = json.dumps(analysis, ensure_ascii=False)
        values[self.field_ids["lib_analysis_version"]] = version
        self.writer.request("PATCH", f"/api/documents/{int(document_id)}/", {"custom_fields": [{"field": key, "value": value} for key, value in values.items()]})
        updated = custom_fields(self.reader.document(document_id))
        if updated.get(self.field_ids["lib_analysis_version"]) != version or updated.get(self.field_ids["lib_analysis"]) != json.dumps(analysis, ensure_ascii=False):
            raise LibraryError("analysis readback mismatch", "native_failed", 502)
        return "updated"

    def verify(self, document_id, item):
        if self.permission_ids is None:
            raise LibraryError("native permission IDs are required", "config_invalid", 409)
        ids = self.permission_ids
        permissions = {"view": {"users": [ids["reader_user_id"], ids["boss_user_id"]], "groups": []},
                       "change": {"users": [ids["writer_user_id"], ids["boss_user_id"]], "groups": []}}
        self.writer.request("PATCH", f"/api/documents/{int(document_id)}/", {"set_permissions": permissions})
        # Writer readback catches a rejected permission patch; reader proves visibility.
        self.writer.document(document_id)
        doc = self.reader.document(document_id); fields = custom_fields(doc)
        return (fields.get(self.field_ids["lib_source_id"]) == item.get("source_id") and
                fields.get(self.field_ids["lib_content_hash"]) == item.get("content_hash") and
                fields.get(self.field_ids["lib_canonical_url"]) == item.get("canonical_url") and
                (item.get("kind") == "file" or bool(item.get("metadata", {}).get("supplement_file_path")) or
                 "── SOURCE ──" in str(doc.get("content") or "")))
