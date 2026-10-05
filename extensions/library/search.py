"""Bounded permission-aware native search and exact original-content evidence."""
from __future__ import annotations

import json
import re
from collections import deque
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .model import Evidence, LibraryError, canonical_url
from .native import NativeClient, custom_fields, strip_analysis

MAX_VARIANTS = 8
PAGE_SIZE = 24
PER_QUERY_CANDIDATES = 12
MAX_CANDIDATES = 96
MAX_DOCUMENT_READS = 32
MAX_HITS = 8
MAX_EXCERPT = 1200
MAX_EVIDENCE_RESPONSE = 12000
MAX_QUERY = 512

ALIASES = {"hermes": ("赫耳墨斯",), "codex": (),
           "context compression": ("上下文壓縮", "上下文压缩"),
           "memory": ("記憶", "记忆"), "mcp": ("Model Context Protocol",),
           "model context protocol": ("MCP",),
           "上下文壓縮": ("context compression",), "上下文压缩": ("context compression",),
           "記憶": ("memory",), "记忆": ("memory",), "赫耳墨斯": ("Hermes",)}
ENGLISH_STOP = {"a", "an", "and", "are", "about", "can", "could", "do", "does", "for", "from",
                "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "the", "this", "to",
                "what", "which", "who", "why", "with", "you", "your"}
CHINESE_STOP = ("這份", "这份", "這個", "这个", "哪些", "什么", "什麼", "如何", "怎麼", "怎么", "請問", "请问",
                "可以", "能夠", "能够", "幫我", "帮我", "一下", "文章", "內容", "内容", "問題", "问题", "關於", "关于",
                "的是", "是否", "以及", "還有", "还有")
SECRET_WORDS = {"token", "password", "passwd", "secret", "credential", "credentials", "api key", "apikey",
                "密碼", "密码", "金鑰", "密钥", "令牌", "憑證", "凭证", "帳密", "账号", "賬號"}
PERSONAL_WORDS = {"my", "mine", "myself", "我", "我的", "我自己", "本人", "自己的", "我帳", "我账"}
WECHAT_TRACKING = {"chksm", "mpshare", "srcid", "sharer_shareinfo"}
UNSAFE_QUERY_PARTS = ("token", "secret", "password", "passwd", "auth", "credential", "session", "xsec", "cookie", "ticket")
IDENTITY_QUERY = {"__biz", "biz", "mid", "idx", "sn", "id", "slug", "article", "article_id", "note_id", "doc", "document"}
SOURCE_OPEN, SOURCE_CLOSE = "── SOURCE ──", "── END SOURCE ──"
OCR_OPEN, OCR_CLOSE = "── OCR ──", "── END OCR ──"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if len(value.strip()) > 1))


def _scripts(value: str) -> list[str]:
    try:
        from opencc import OpenCC
        return _unique([value, OpenCC("s2t").convert(value), OpenCC("t2s").convert(value)])
    except ImportError:
        return [value]


def _chinese_terms(query: str) -> list[str]:
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    cleaned = []
    for chunk in chunks:
        for word in CHINESE_STOP:
            chunk = chunk.replace(word, " ")
        cleaned.extend(part for part in chunk.split() if len(part) >= 2)
    out = list(cleaned)
    for part in cleaned:
        if len(part) > 4:
            out.extend(part[index:index + 3] for index in range(len(part) - 2))
        if len(part) > 2:
            out.extend(part[index:index + 2] for index in range(len(part) - 1))
    return _unique(out)


def variants(query: str) -> list[str]:
    """Favor precise phrases while reserving Chinese terms in both scripts."""
    raw_tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", query.lower())
    english = _unique([word for word in raw_tokens if word not in ENGLISH_STOP])
    phrase = " ".join(raw_tokens)
    phrases = [alias for alias in ALIASES if " " in alias and alias in phrase]
    if len(english) >= 2 and not phrases:
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)+", query):
            words = [word.lower() for word in match.group().split() if word.lower() not in ENGLISH_STOP]
            if len(words) >= 2: phrases.append(" ".join(words))
    chinese = _chinese_terms(query)
    phrase_parts = {word for value in phrases for word in value.split()}
    alias_values = [alias for key in _unique(phrases + english + chinese) for alias in ALIASES.get(key, ())]
    english_aliases = [alias for alias in alias_values if re.search(r"[A-Za-z]", alias)]
    chinese_aliases = [alias for alias in alias_values if re.search(r"[\u4e00-\u9fff]", alias)]
    precise = _unique(phrases + [word for word in english if word not in phrase_parts] +
                      english_aliases + [word for word in english if word in phrase_parts])
    translated = _unique([form for value in chinese + chinese_aliases for form in _scripts(value)])
    reserved = 2 if translated else 0
    chosen = precise[:MAX_VARIANTS - reserved]
    if translated:
        first = chinese[0] if chinese else chinese_aliases[0]
        chosen.extend(_scripts(first))
        chosen.extend(translated)
    return _unique(chosen)[:MAX_VARIANTS]


def _is_private_secret_request(query: str) -> bool:
    lower = query.lower()
    return any(re.search(r"\b" + re.escape(word) + r"\b", lower) if word.isascii() else word in lower
               for word in PERSONAL_WORDS) and any(word in lower for word in SECRET_WORDS)


def _validate_query(query: str) -> None:
    if not isinstance(query, str) or not query.strip(): raise LibraryError("query is required")
    if len(query) > MAX_QUERY: raise LibraryError("query exceeds 512 characters")


def _citation(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip(): return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
            return None
        parts.port
        pairs = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
                 if key.lower() in IDENTITY_QUERY and key.lower() not in WECHAT_TRACKING
                 and not any(part in key.lower() for part in UNSAFE_QUERY_PARTS)]
        sanitized = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))
        return canonical_url(sanitized)
    except (ValueError, LibraryError):
        return None


def _spans(content: str) -> list[tuple[str, int, int]]:
    source = strip_analysis(content)
    if SOURCE_OPEN not in source and OCR_OPEN not in source:
        return [("source", 0, len(source))] if source else []
    spans = []
    for label, opening, closing in (("source", SOURCE_OPEN, SOURCE_CLOSE), ("ocr", OCR_OPEN, OCR_CLOSE)):
        at = source.find(opening)
        if at < 0: continue
        start = at + len(opening)
        end = source.find(closing, start)
        if end < 0:
            following = source.find(OCR_OPEN, start) if label == "source" else -1
            end = following if following >= 0 else len(source)
        if start < end: spans.append((label, start, end))
    return spans


def _anchors(query: str) -> list[tuple[str, int]]:
    lower = query.lower()
    english = [word for word in re.findall(r"[a-z][a-z0-9_-]*", lower) if word not in ENGLISH_STOP]
    phrases = [key for key in ALIASES if " " in key and key in lower]
    if len(english) > 1: phrases.append(" ".join(english))
    result = [(word, 24) for word in _unique(phrases)]
    result += [(word, 16 if len(word) >= 3 else 12) for word in _unique(english)]
    for key in _unique(phrases + english):
        result.extend((alias.lower(), 12) for alias in ALIASES.get(key, ()))
    for word in _chinese_terms(query):
        for form in _scripts(word):
            result.append((form.lower(), 8 if len(form) >= 3 else 3))
            result.extend((alias.lower(), 12) for alias in ALIASES.get(form, ()))
    return list(dict.fromkeys(result))[:48]


def _contains(content: str, term: str) -> bool:
    if re.fullmatch(r"[a-z][a-z0-9_-]*(?: [a-z][a-z0-9_-]*)*", term):
        return re.search(r"(?<![a-z0-9_-])" + re.escape(term) + r"(?![a-z0-9_-])", content) is not None
    return term in content


def _concept_groups(query: str) -> list[frozenset[str]]:
    """Each English concept needs its own exact or fixed-alias support."""
    lower = query.lower()
    words = _unique([word for word in re.findall(r"[a-z][a-z0-9_-]*", lower)
                     if word not in ENGLISH_STOP])
    phrases = [key for key in ALIASES if " " in key and key.isascii() and _contains(lower, key)]
    phrase_words = {word for phrase in phrases for word in phrase.split()}
    groups: list[set[str]] = []
    for key in phrases + [word for word in words if word not in phrase_words]:
        terms = {key, *(alias.lower() for alias in ALIASES.get(key, ()))}
        for term in list(terms):
            terms.update(alias.lower() for alias in ALIASES.get(term, ()))
        overlapping = [group for group in groups if group & terms]
        if overlapping:
            for group in overlapping: groups.remove(group); terms.update(group)
        groups.append(terms)
    return [frozenset(group) for group in groups]


def _covered_concepts(groups: list[frozenset[str]], source: str, title: str) -> set[int]:
    return {index for index, alternatives in enumerate(groups)
            if any(_contains(source, term) or _contains(title, term) for term in alternatives)}


def _metadata(document: dict, ids: dict[str, int], legacy_ids: dict[str, int]) -> dict:
    fields = custom_fields(document)
    enrichment = fields.get(legacy_ids.get("wx_enrichment"))
    try: enrichment = json.loads(enrichment) if isinstance(enrichment, str) else enrichment
    except (ValueError, TypeError): enrichment = None
    if not isinstance(enrichment, dict): enrichment = {}
    return {"canonical_url": _citation(fields.get(ids.get("lib_canonical_url"))) or _citation(fields.get(legacy_ids.get("wx_source_url"))),
            "author": fields.get(ids.get("lib_author")) or (enrichment.get("author") if isinstance(enrichment.get("author"), str) else None),
            "published_at": fields.get(ids.get("lib_publish_date")) or fields.get(legacy_ids.get("wx_publish_date")),
            "fetched_at": fields.get(ids.get("lib_fetched_at")),
            "completeness": fields.get(ids.get("lib_completeness")) or "unknown"}


def _evidence(document: dict, ids: dict[str, int], legacy_ids: dict[str, int], query: str) -> list[dict]:
    raw = str(document.get("content") or "")
    anchors = _anchors(query)
    if not raw or not anchors: return []
    spans = _spans(raw)
    source_text = " ".join(raw[left:right] for _, left, right in spans).lower()
    groups = _concept_groups(query)
    source_coverage = _covered_concepts(groups, source_text, "")
    title_coverage = _covered_concepts(groups, "", str(document.get("title") or "").lower())
    if groups and not (source_coverage or title_coverage):
        return []
    meta = _metadata(document, ids, legacy_ids)
    lower = raw.lower()
    positions = []
    for section, left, right in spans:
        for word, weight in anchors:
            section_text = lower[left:right]
            if re.fullmatch(r"[a-z][a-z0-9_-]*(?: [a-z][a-z0-9_-]*)*", word):
                offsets = (match.start() for match in re.finditer(r"(?<![a-z0-9_-])" + re.escape(word) + r"(?![a-z0-9_-])", section_text))
            else:
                def occurrences():
                    at = section_text.find(word)
                    while at >= 0:
                        yield at
                        at = section_text.find(word, at + len(word))
                offsets = occurrences()
            first, last = [], deque(maxlen=6)
            for at in offsets:
                if len(first) < 6: first.append(at)
                else: last.append(at)
            positions.extend((left + at, section, left, right, weight) for at in first + list(last))
    strong_chinese = any(weight >= 8 and re.search(r"[\u4e00-\u9fff]", word)
                         and _contains(source_text, word) for word, weight in anchors)
    if not positions or (groups and not source_coverage and not strong_chinese):
        return []
    windows = []
    for at, section, left, right, weight in positions:
        start = max(left, at - 180)
        end = min(right, max(at + 240, start + 420), start + MAX_EXCERPT)
        fragment = lower[start:end]
        coverage = sum(w for term, w in anchors if _contains(fragment, term))
        windows.append((coverage + weight, start, end, section))
    windows.sort(key=lambda item: (-item[0], item[1]))
    selected = []
    for _, start, end, section in windows:
        if any(section == old_section and start < old_end and end > old_start for old_start, old_end, old_section in selected):
            continue
        selected.append((start, end, section))
        if len(selected) == MAX_HITS: break
    hits = [Evidence(int(document["id"]), meta["canonical_url"], str(document.get("title") or ""),
                     meta["author"], meta["published_at"], meta["fetched_at"], meta["completeness"],
                     section, start, end, raw[start:end]).as_dict() for start, end, section in selected]
    while hits and len(json.dumps({"document_id": int(document["id"]), "query": query,
                                   "hits": hits, "no_match": False}, ensure_ascii=True)) > MAX_EVIDENCE_RESPONSE:
        hits.pop()
    return hits


def evidence_for(document: dict, ids: dict[str, int], query: str) -> list[dict]:
    """Public compatibility helper; LibrarySearch supplies legacy field IDs itself."""
    _validate_query(query)
    return _evidence(document, ids, {}, query)


class LibrarySearch:
    def __init__(self, reader: Any, field_ids: dict[str, int], legacy_field_ids: dict[str, int] | None = None):
        self.reader, self.field_ids, self.legacy_field_ids = reader, field_ids, legacy_field_ids or {}

    def _rows(self, term: str) -> list[dict]:
        if isinstance(self.reader, NativeClient):
            result = self.reader.request("GET", "/api/documents/?" + urlencode({"search": term, "page_size": PAGE_SIZE}))
            if not isinstance(result, dict) or not isinstance(result.get("results"), list):
                raise LibraryError("invalid native search response", "native_failed", 502)
            return result["results"][:PAGE_SIZE]
        return self.reader.search(term)[:PAGE_SIZE]

    def search(self, query: str, limit: int = 8) -> dict:
        _validate_query(query)
        if not isinstance(limit, int) or not 1 <= limit <= 8: raise LibraryError("limit must be 1-8")
        used = variants(query)
        if _is_private_secret_request(query): return {"variants": used, "matches": [], "no_match": True}
        candidates: dict[int, dict] = {}
        anchors = _anchors(query)
        groups = _concept_groups(query)
        for index, term in enumerate(used):
            rows = self._rows(term)
            rows.sort(key=lambda row: -sum(weight for word, weight in anchors
                                             if _contains(str(row.get("title") or "").lower(), word))
                      if isinstance(row, dict) else 0)
            for row in rows[:PER_QUERY_CANDIDATES]:
                if not isinstance(row, dict) or not isinstance(row.get("id"), int): continue
                ident = row["id"]
                if ident not in candidates and len(candidates) >= MAX_CANDIDATES: continue
                entry = candidates.setdefault(ident, {"row": row, "terms": set(), "first": index})
                entry["terms"].add(index)
        def priority(item: tuple[int, dict]) -> tuple:
            ident, entry = item
            title = str(entry["row"].get("title") or "").lower()
            title_score = sum(weight for word, weight in anchors if _contains(title, word))
            return (-title_score, -len(entry["terms"]), entry["first"], ident)
        matches = []
        concept_coverage = {}
        for ident, entry in sorted(candidates.items(), key=priority)[:MAX_DOCUMENT_READS]:
            document = self.reader.document(ident)
            hits = _evidence(document, self.field_ids, self.legacy_field_ids, query)
            if not hits: continue
            raw = str(document.get("content") or "")
            source_text = " ".join(raw[left:right] for _, left, right in _spans(raw)).lower()
            title = str(document.get("title") or "")
            title_lower = title.lower()
            covered = _covered_concepts(groups, source_text, title_lower)
            if groups and not covered: continue
            present = [(word, weight) for word, weight in anchors if _contains(source_text, word)]
            if not present: continue
            phrase_score = sum(weight for word, weight in present if " " in word)
            title_score = sum(weight for word, weight in present if _contains(title_lower, word))
            english_score = sum(weight for word, weight in present if re.fullmatch(r"[a-z][a-z0-9_-]*", word))
            coverage = sum(weight for _, weight in present)
            score = phrase_score * 6 + title_score * 4 + english_score * 3 + coverage
            matches.append({"id": ident, "title": title, "summary": hits[0]["excerpt"][:360],
                            "score": score, "canonical_url": hits[0]["canonical_url"],
                            "published_at": hits[0]["published_at"], "completeness": hits[0]["completeness"]})
            concept_coverage[ident] = covered
        matches.sort(key=lambda hit: (-hit["score"], hit["title"].casefold(), hit["id"]))
        selected, remaining = [], list(matches)
        uncovered = set(range(len(groups)))
        while uncovered and remaining and len(selected) < limit:
            best = max(range(len(remaining)),
                       key=lambda index: (len(concept_coverage[remaining[index]["id"]] & uncovered), -index))
            hit = remaining.pop(best)
            newly_covered = concept_coverage[hit["id"]] & uncovered
            if not newly_covered: break
            selected.append(hit)
            uncovered -= newly_covered
        if uncovered:
            selected = []
        else:
            selected.extend(remaining[:limit - len(selected)])
            selected.sort(key=lambda hit: (-hit["score"], hit["title"].casefold(), hit["id"]))
        return {"variants": used, "matches": selected, "no_match": not selected}

    def evidence(self, document_id: int, query: str) -> dict:
        _validate_query(query)
        document = self.reader.document(document_id)
        hits = [] if _is_private_secret_request(query) else _evidence(document, self.field_ids, self.legacy_field_ids, query)
        return {"document_id": int(document_id), "query": query, "hits": hits, "no_match": not hits}
