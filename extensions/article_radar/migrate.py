#!/usr/bin/env python3
"""Bounded, resumable import from the read-only article library to Paperless."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from radar import (FIELDS, VERSION, archive_text, existing_keys, field_values,
                   normalize_page, raw_by_url, select_articles, source_id,
                   validate_upstream, verify_document)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RECEIPT = Path.home() / "Library/Application Support/paperless-radar/migration-receipt.json"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_secret(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("secret file must be a regular external file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("secret file must have mode 0600")
    tokens = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "PAPERLESS_API_TOKEN":
            raise ValueError("secret file must contain only PAPERLESS_API_TOKEN")
        value = value.strip()
        if value.startswith(('"', "'")) or value.endswith(('"', "'")):
            if len(value) < 2 or value[0] != value[-1]:
                raise ValueError("invalid API token quoting")
            value = value[1:-1]
        if not value or any(ch.isspace() for ch in value) or "$" in value:
            raise ValueError("invalid API token")
        tokens.append(value)
    if len(tokens) != 1:
        raise ValueError("secret file must contain exactly one API token")
    return tokens[0]


def validate_receipt_path(path: Path, library: Path) -> Path:
    real = path.expanduser().resolve()
    if real.is_relative_to(ROOT) or real.is_relative_to(library.resolve()):
        raise ValueError("receipt must be outside repository and source library")
    if path.is_symlink():
        raise ValueError("receipt symlink is not allowed")
    return real


def save_receipt(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp.exists():
            temp.unlink()


def load_receipt(path: Path, upstream: str) -> dict:
    if not path.exists():
        return {"version": VERSION, "upstream": upstream, "entries": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("version") != VERSION or
            data.get("upstream") != upstream or not isinstance(data.get("entries"), dict)):
        raise ValueError("receipt version/upstream mismatch")
    return data


class PaperlessAPI:
    def __init__(self, base: str, token: str):
        self.base = validate_upstream(base)
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, method: str, path: str, payload=None, content_type="application/json"):
        if not path.startswith("/api/") or ".." in path or "\\" in path:
            raise ValueError("invalid API path")
        data = None
        headers = {"Authorization": "Token " + self.token,
                   "Accept": "application/json; version=10"}
        if payload is not None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = content_type
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                blob = response.read(16 * 1024 * 1024 + 1)
                if len(blob) > 16 * 1024 * 1024:
                    raise ValueError("API response exceeds limit")
                return json.loads(blob)
        except HTTPError as exc:
            raise ValueError(f"Paperless API returned HTTP {exc.code} for {method} {path.split('?')[0]}") from None

    def pages(self, path: str) -> list[dict]:
        rows, visited = [], set()
        while path:
            if path in visited or len(visited) >= 1000:
                raise ValueError("API pagination loop or excessive pages")
            visited.add(path)
            page, next_url = normalize_page(self.request("GET", path))
            rows.extend(page)
            if next_url:
                parts = urlsplit(next_url)
                if parts.scheme or parts.netloc:
                    if f"{parts.scheme}://{parts.netloc}" != self.base:
                        raise ValueError("API pagination escaped fixed upstream")
                    path = parts.path + ("?" + parts.query if parts.query else "")
                else:
                    path = next_url
            else:
                path = None
        return rows


def get_or_create(api: PaperlessAPI, endpoint: str, name: str, expected_type=None) -> int:
    rows = api.pages(endpoint)
    matches = [row for row in rows if row.get("name") == name]
    if len(matches) > 1:
        raise ValueError(f"ambiguous native resource: {name}")
    if matches:
        row = matches[0]
        if expected_type and row.get("data_type") != expected_type:
            raise ValueError(f"custom field type mismatch: {name}")
        return int(row["id"])
    payload = {"name": name}
    if expected_type:
        payload["data_type"] = expected_type
    row = api.request("POST", endpoint, payload)
    if row.get("name") != name or (expected_type and row.get("data_type") != expected_type):
        raise ValueError(f"created native resource readback mismatch: {name}")
    return int(row["id"])


def multipart(item: dict, body: str, values: dict, field_ids: dict,
              correspondent_id: int | None, document_type_id: int,
              tag_ids: set[int]) -> tuple[bytes, str]:
    boundary = "ArticleRadar" + uuid.uuid4().hex
    parts = []
    def add(name, value, filename=None, mime=None):
        header = f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
        if filename:
            header += f"; filename=\"{filename}\""
        header += "\r\n"
        if mime:
            header += f"Content-Type: {mime}\r\n"
        parts.append(header.encode() + b"\r\n" +
                     (value if isinstance(value, bytes) else str(value).encode("utf-8")) + b"\r\n")
    add("document", body.encode("utf-8"), "article.txt", "text/plain; charset=utf-8")
    add("title", item["title"])
    if correspondent_id is not None:
        add("correspondent", correspondent_id)
    add("document_type", document_type_id)
    for tag_id in sorted(tag_ids):
        add("tags", tag_id)
    if values.get("wx_publish_date"):
        add("created", values["wx_publish_date"] + "T12:00:00")
    add("custom_fields", json.dumps({str(field_ids[k]): v for k, v in values.items()}, ensure_ascii=False))
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def task_document_id(api: PaperlessAPI, task_uuid: str, *, sleep=time.sleep) -> int:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        tasks = api.pages("/api/tasks/?" + urlencode({"task_id": task_uuid}))
        matches = [task for task in tasks if task.get("task_id") == task_uuid]
        if len(matches) > 1:
            raise ValueError("ambiguous task UUID")
        if matches:
            task = matches[0]
            status = str(task.get("status") or "").lower()
            if status in {"failure", "revoked"}:
                if (task.get("result_data") or {}).get("duplicate_of"):
                    raise ValueError("ingestion task rejected duplicate content; receipt requires manual resolution")
                raise ValueError(f"ingestion task ended with {status}")
            if status == "success":
                doc_id = (task.get("result_data") or {}).get("document_id")
                if not isinstance(doc_id, int) or doc_id <= 0:
                    raise ValueError("successful task lacks document_id")
                return doc_id
        sleep(2)
    raise TimeoutError("ingestion task did not complete within 10 minutes")


def resume_pending(api, receipt, receipt_path, item_by_key, raw, field_ids,
                   correspondent_ids, document_type_id, tag_ids_by_key):
    for key, entry in receipt["entries"].items():
        state = entry.get("state")
        if state == "intent":
            raise ValueError("uncertain upload intent in receipt; resolve manually before retry")
        if state not in {"task", "confirmed"}:
            raise ValueError("invalid or failed receipt state; resolve manually")
        if state == "confirmed":
            continue
        item = item_by_key.get(key)
        if not item or entry.get("url") != item.get("url"):
            raise ValueError("pending receipt source is unavailable or changed")
        doc_id = task_document_id(api, entry["task_uuid"])
        values = field_values(item)
        doc = api.request("GET", f"/api/documents/{doc_id}/")
        verify_document(doc, item, raw[item["url"]], values, field_ids,
                        correspondent_ids.get(item.get("account")), document_type_id,
                        tag_ids_by_key[key])
        entry.update(state="confirmed", document_id=doc_id)
        save_receipt(receipt_path, receipt)


def run(args) -> dict:
    upstream = validate_upstream(args.base_url)
    library = Path(args.library).expanduser().resolve()
    receipt_path = validate_receipt_path(Path(args.receipt), library)
    raw_file, enriched_file = library / "data/articles_raw.jsonl", library / "app/library.json"
    with raw_file.open(encoding="utf-8") as handle:
        raw = raw_by_url(handle)
    items = json.loads(enriched_file.read_text(encoding="utf-8"))["items"]
    selected, skipped = select_articles(items, raw, args.limit)
    if args.dry_run:
        return {"selected": len(selected), "skipped_nonarticle_or_missing": skipped, "dry_run": True}
    input_secret_path = Path(args.secret_file).expanduser()
    if input_secret_path.is_symlink():
        raise ValueError("secret file symlink is not allowed")
    secret_path = input_secret_path.resolve()
    if secret_path.is_relative_to(ROOT) or secret_path.is_relative_to(library):
        raise ValueError("secret file must be outside repository and source library")
    token = read_secret(secret_path)
    api = PaperlessAPI(upstream, token)
    receipt = load_receipt(receipt_path, upstream)
    # An unresolved intent must block even resource creation.
    if any(entry.get("state") == "intent" for entry in receipt["entries"].values()):
        raise ValueError("uncertain upload intent in receipt; resolve manually before retry")
    fields = {name: get_or_create(api, "/api/custom_fields/", name, kind)
              for name, kind in FIELDS.items()}
    document_type_id = get_or_create(api, "/api/document_types/", "WeChat article")
    all_items = {source_id(i): i for i in items if isinstance(i, dict) and i.get("url")}
    pending_items = [all_items[k] for k, v in receipt["entries"].items() if v.get("state") == "task" and k in all_items]
    relevant = selected + pending_items
    correspondents = {name: get_or_create(api, "/api/correspondents/", name)
                      for name in sorted({str(i.get("account")) for i in relevant if i.get("account")})}
    tag_names = {"wechat"}
    for item in relevant:
        tag_names.update(str(x) for x in (item.get("tags") or []) if str(x).strip())
        if item.get("category"):
            tag_names.add(str(item["category"]))
    tags = {name: get_or_create(api, "/api/tags/", name) for name in sorted(tag_names)}
    tag_ids_by_key = {}
    for item in relevant:
        names = {"wechat", *[str(x) for x in (item.get("tags") or []) if str(x).strip()]}
        if item.get("category"):
            names.add(str(item["category"]))
        tag_ids_by_key[source_id(item)] = {tags[name] for name in names}
    resume_pending(api, receipt, receipt_path, all_items, raw, fields,
                   correspondents, document_type_id, tag_ids_by_key)
    docs = api.pages("/api/documents/?page_size=100&fields=id,custom_fields")
    existing_ids, existing_urls = existing_keys(docs, fields)
    uploaded = duplicates = 0
    for item in selected:
        key, url = source_id(item), item["url"].strip()
        if key in existing_ids or url in existing_urls:
            duplicates += 1
            continue
        if key in receipt["entries"]:
            raise ValueError("receipt entry has no matching native document; resolve manually")
        values = field_values(item)
        body = raw[url]
        content = archive_text(item, body)
        payload, content_type = multipart(item, content, values, fields,
                                          correspondents.get(item.get("account")),
                                          document_type_id, tag_ids_by_key[key])
        receipt["entries"][key] = {"state": "intent", "url": url}
        save_receipt(receipt_path, receipt)
        task_uuid = api.request("POST", "/api/documents/post_document/", payload, content_type)
        try:
            uuid.UUID(task_uuid)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("upload did not return a task UUID") from None
        receipt["entries"][key].update(state="task", task_uuid=task_uuid)
        save_receipt(receipt_path, receipt)
        doc_id = task_document_id(api, task_uuid)
        doc = api.request("GET", f"/api/documents/{doc_id}/")
        verify_document(doc, item, body, values, fields,
                        correspondents.get(item.get("account")), document_type_id,
                        tag_ids_by_key[key])
        receipt["entries"][key].update(state="confirmed", document_id=doc_id)
        save_receipt(receipt_path, receipt)
        existing_ids.add(key)
        existing_urls.add(url)
        uploaded += 1
    return {"selected": len(selected), "skipped_nonarticle_or_missing": skipped,
            "uploaded": uploaded, "duplicates": duplicates}


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a bounded Article Radar batch")
    parser.add_argument("--library", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--base-url", default="http://127.0.0.1:4386")
    parser.add_argument("--secret-file")
    parser.add_argument("--receipt", default=str(DEFAULT_RECEIPT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.secret_file:
        parser.error("--secret-file is required unless --dry-run is used")
    try:
        print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, TimeoutError, OSError, KeyError, TypeError) as exc:
        print(f"Article Radar import stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
