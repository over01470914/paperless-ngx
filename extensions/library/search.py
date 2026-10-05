"""Bounded native-reader search and verbatim evidence extraction."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .model import Evidence, LibraryError, canonical_url
from .native import custom_fields, strip_analysis

ALIASES = {"hermes": ["Hermes", "赫耳墨斯"], "codex": ["Codex"],
           "context compression": ["context compression", "上下文壓縮"],
           "memory": ["memory", "記憶"], "mcp": ["MCP", "Model Context Protocol"]}


def variants(query: str) -> list[str]:
    english = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", query.lower())
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    # Preserve precise English terms first (e.g. witr), then fixed aliases,
    # then Chinese clauses/bigrams and both script directions under one budget.
    out = list(english)
    for token in english: out.extend(ALIASES.get(token, []))
    out.extend(chinese)
    try:
        from opencc import OpenCC
        for mode in ("s2t", "t2s"):
            converted = OpenCC(mode).convert(query)
            out.extend(re.findall(r"[\u4e00-\u9fff]{2,}", converted))
    except Exception:
        pass
    for value in list(out):
        chars = "".join(re.findall(r"[\u4e00-\u9fff]", value))
        out.extend(chars[index:index + 2] for index in range(min(max(0, len(chars) - 1), 4)))
    return list(dict.fromkeys(value for value in out if len(value) > 1))[:8]


def evidence_for(document: dict, ids: dict[str, int], query: str) -> list[dict]:
    fields, content = custom_fields(document), strip_analysis(str(document.get("content") or ""))
    words = []
    for value in variants(query):
        words.extend(word for word in re.findall(r"[\w-]+|[\u4e00-\u9fff]{2,}", value.lower()) if len(word) > 1)
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", value))
        words.extend(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    words = list(dict.fromkeys(words))[:20]
    if not words or not content: return []
    lower, hits = content.lower(), []
    for word in words:
        start = 0
        while len(hits) < 8:
            at = lower.find(word, start)
            if at < 0: break
            begin, end = max(0, at - 160), min(len(content), at + len(word) + 240)
            end = min(end, begin + 1200)
            excerpt = content[begin:end]
            if excerpt and all(existing["start"] != begin for existing in hits):
                hit = Evidence(int(document["id"]), fields.get(ids["lib_canonical_url"]), str(document.get("title") or ""),
                               fields.get(ids["lib_author"]), fields.get(ids["lib_publish_date"]), fields.get(ids["lib_fetched_at"]),
                               fields.get(ids["lib_completeness"]) or "unknown", "source", begin, end, excerpt).as_dict(); hits.append(hit)
            start = at + len(word)
    return hits


class LibrarySearch:
    def __init__(self, reader: Any, field_ids: dict[str, int], legacy_field_ids: dict[str, int] | None = None):
        self.reader, self.field_ids, self.legacy_field_ids = reader, field_ids, legacy_field_ids or {}
    def _legacy(self, document: dict) -> dict:
        fields = custom_fields(document)
        original = fields.get(self.legacy_field_ids.get("wx_source_url"))
        enrichment = fields.get(self.legacy_field_ids.get("wx_enrichment"))
        try: enrichment = __import__("json").loads(enrichment) if isinstance(enrichment, str) else {}
        except ValueError: enrichment = {}
        return {"canonical_url": canonical_url(original) if isinstance(original, str) else None,
                "published_at": fields.get(self.legacy_field_ids.get("wx_publish_date")),
                "author": enrichment.get("author") if isinstance(enrichment, dict) and isinstance(enrichment.get("author"), str) else None}
    def search(self, query: str, limit: int = 8) -> dict:
        if not isinstance(query, str) or not query.strip(): raise LibraryError("query is required")
        if not 1 <= limit <= 8: raise LibraryError("limit must be 1-8")
        used, candidates = variants(query), {}
        for term in used:
            for row in self.reader.search(term): candidates[int(row["id"])] = row
        matches = []
        for row in candidates.values():
            document = self.reader.document(int(row["id"]))
            evidence = evidence_for(document, self.field_ids, query)
            if evidence:
                fields, legacy = custom_fields(document), self._legacy(document); matches.append({"id": int(row["id"]), "title": document.get("title") or "",
                    "summary": evidence[0]["excerpt"][:360], "score": len(evidence),
                    "canonical_url": fields.get(self.field_ids["lib_canonical_url"]) or legacy["canonical_url"],
                    "published_at": fields.get(self.field_ids["lib_publish_date"]) or legacy["published_at"],
                    "completeness": fields.get(self.field_ids["lib_completeness"]) or "unknown"})
        matches.sort(key=lambda value: (-value["score"], value["id"]))
        return {"variants": used, "matches": matches[:limit], "no_match": not matches}
    def evidence(self, document_id: int, query: str) -> dict:
        document = self.reader.document(document_id)
        hits = evidence_for(document, self.field_ids, query)
        legacy = self._legacy(document)
        for hit in hits:
            if hit["canonical_url"] is None: hit["canonical_url"] = legacy["canonical_url"]
            if hit["author"] is None: hit["author"] = legacy["author"]
            if hit["published_at"] is None: hit["published_at"] = legacy["published_at"]
        return {"document_id": int(document_id), "query": query, "hits": hits, "no_match": not hits}
