"""Runtime composition.  Read external config only when launched by parent."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .ledger import Ledger
from .legacy import LegacyMigrator
from .native import NativeClient, NativeIngestor
from .enrich import ApprovedProvider
from .search import LibrarySearch
from .service import LibraryService, serve
from .worker import LibraryWorker
from .sources import IMAGE_CDN, SafeFetcher, URLGuard, pinned_transport
from .local_ocr import LocalOCR


def load_config(path: str | None = None) -> dict:
    value = Path(path or os.environ.get("PAPERLESS_LIBRARY_CONFIG", "~/.config/paperless-ngx/library.json")).expanduser()
    data = json.loads(value.read_text(encoding="utf-8"))
    required = ("native_url", "reader_token", "writer_token", "service_token", "runtime_dir", "allowed_file_roots", "legacy_library", "service_url", "rate_limit_seconds", "reader_user_id", "writer_user_id", "boss_user_id")
    if any(key not in data for key in required) or not isinstance(data["allowed_file_roots"], list): raise ValueError("invalid library runtime config")
    if data["service_url"] != "http://127.0.0.1:4388" or float(data["rate_limit_seconds"]) < 1.5: raise ValueError("invalid local service configuration")
    return data


def build(config: dict):
    runtime = Path(config["runtime_dir"]).expanduser()
    ledger = Ledger(runtime / "jobs.sqlite", runtime / "staging")
    reader, writer = NativeClient(config["native_url"], config["reader_token"]), NativeClient(config["native_url"], config["writer_token"])
    field_ids = writer.ensure_schema()
    resources = {"document_type": writer.ensure_named_resource("/api/document_types/", "Library source"),
                 "tag": writer.ensure_named_resource("/api/tags/", "Library")}
    legacy_field_ids = {row["name"]: int(row["id"]) for row in reader.pages("/api/custom_fields/")
                        if row.get("name") in {"wx_source_url", "wx_source_id", "wx_publish_date", "wx_enrichment"} and isinstance(row.get("id"), int)}
    provider = None
    if config.get("enrichment_enabled"):
        provider = ApprovedProvider(config.get("enrichment_endpoint"), config.get("enrichment_model"), config.get("enrichment_key_file"), config.get("enrichment_version", "approved-1"))
    native = NativeIngestor(reader, writer, field_ids, resources,
                            {key: int(config[key]) for key in ("reader_user_id", "writer_user_id", "boss_user_id")}, provider, legacy_field_ids)
    worker = LibraryWorker(ledger, native, fetcher=SafeFetcher(URLGuard(), pinned_transport),
                           ocr=LocalOCR(SafeFetcher(URLGuard(allowed_hosts=IMAGE_CDN, secure_only=True), pinned_transport)),
                           accepted_roots=config["allowed_file_roots"], rate_limit_seconds=config["rate_limit_seconds"])
    migrator = LegacyMigrator(ledger, config["legacy_library"])
    return LibraryService(ledger, worker, LibrarySearch(reader, field_ids, legacy_field_ids), writer, field_ids, config["allowed_file_roots"], migrator)


def main():
    config = load_config(); service = build(config); server = serve(service, config["service_token"])
    try: server.serve_forever()
    finally: service.stop_worker(); server.server_close()


if __name__ == "__main__": main()
