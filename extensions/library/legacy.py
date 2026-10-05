"""Read-only, bounded migration from the fixed historical source library."""
from __future__ import annotations

import json
from pathlib import Path

from .model import LibraryError


class LegacyMigrator:
    def __init__(self, ledger, library_path: str):
        self.ledger, self.root = ledger, Path(library_path).expanduser().resolve()
    def __call__(self, limit=None):
        raw_file = self.root / "data" / "articles_raw.jsonl"
        if not raw_file.is_file(): raise LibraryError("fixed legacy raw library is unavailable", "migration_unavailable", 409)
        if limit is not None and (not isinstance(limit, int) or not 1 <= limit <= 500): raise LibraryError("migration limit must be 1-500")
        enriched = {}
        index_file = self.root / "app" / "library.json"
        if index_file.is_file():
            try:
                for row in json.loads(index_file.read_text(encoding="utf-8")).get("items", []):
                    if isinstance(row, dict) and isinstance(row.get("url"), str): enriched[row["url"]] = row
            except (OSError, json.JSONDecodeError): pass
        entries, skipped = [], 0
        with raw_file.open(encoding="utf-8") as handle:
            for line in handle:
                try: row = json.loads(line); text = row.get("text"); source_url = row.get("final_url") or row.get("url")
                except json.JSONDecodeError: skipped += 1; continue
                if not isinstance(text, str) or not text.strip(): skipped += 1; continue
                old = enriched.get(source_url, {})
                legacy_analysis = {key: old[key] for key in ("summary", "key_points", "category", "actionable", "hype", "shelf_life") if key in old}
                entries.append({"kind": "text", "spool_path": self.ledger.spool(text),
                                "metadata": {"title": row.get("title") if isinstance(row.get("title"), str) else None,
                                             "source_url": source_url if isinstance(source_url, str) else None,
                                             "legacy_analysis": legacy_analysis or None,
                                             "legacy": True}})
                if limit and len(entries) >= limit: break
        if not entries: raise LibraryError("legacy library has no readable content", "migration_empty", 409)
        result = self.ledger.create_batch(entries)
        result["skipped_taxonomy_or_unreadable"] = skipped
        return result
