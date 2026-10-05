"""Durable private job ledger.  Bodies live only in spool files."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .model import MAX_BATCH_ITEMS, STATUSES, TERMINAL, LibraryError, now, safe_error, safe_locator


class Ledger:
    def __init__(self, path: str | Path, spool_dir: str | Path):
        self.path, self.spool_dir = Path(path).expanduser(), Path(spool_dir).expanduser()
        self._prepare_private_dir(self.path.parent)
        self._prepare_private_dir(self.spool_dir)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        os.chmod(self.path, 0o600)
        self._schema()

    @staticmethod
    def _prepare_private_dir(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise LibraryError("runtime path must be a real directory")
        os.chmod(path, 0o700)

    def _schema(self) -> None:
        self.db.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS batches (
          id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL,
          status TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS items (
          id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id), ordinal INTEGER NOT NULL,
          kind TEXT NOT NULL, private_locator TEXT, public_locator TEXT, spool_path TEXT,
          status TEXT NOT NULL, stage TEXT NOT NULL, reason TEXT, document_id INTEGER,
          task_uuid TEXT, source_id TEXT, canonical_url TEXT, content_hash TEXT,
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0, lease_owner TEXT, lease_until REAL, metadata TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(batch_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS item_claim ON items(status, lease_until, created_at);
        CREATE TABLE IF NOT EXISTS worker_lease (name TEXT PRIMARY KEY, owner TEXT NOT NULL, until REAL NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(items)")}
        if "next_attempt_at" not in columns: self.db.execute("ALTER TABLE items ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0")

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except Exception:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def spool(self, body: str | bytes) -> str:
        name = uuid.uuid4().hex + ".spool"
        target = self.spool_dir / name
        fd, created = None, False
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(fd, "wb") as handle:
                fd = None  # ``handle`` owns the descriptor after fdopen.
                handle.write(body.encode("utf-8") if isinstance(body, str) else body)
                handle.flush(); os.fsync(handle.fileno())
        except Exception:
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
            if created and target.parent == self.spool_dir and target.is_file() and not target.is_symlink():
                try: target.unlink()
                except OSError: pass
            raise
        return str(target)

    def discard_unreferenced_spools(self, values: Iterable[str]) -> None:
        """Discard only this request's private staging files after a DB reference check.

        A failed reference check deliberately preserves every candidate: a leak is
        recoverable, while deleting a committed item's source is not.
        """
        candidates = {Path(value) for value in values if isinstance(value, str)}
        candidates = {path for path in candidates if path.parent == self.spool_dir}
        if not candidates: return
        try:
            with self.tx() as db:
                marks = ",".join("?" for _ in candidates)
                rows = db.execute(f"SELECT spool_path FROM items WHERE spool_path IN ({marks})", tuple(map(str, candidates))).fetchall()
                referenced = {Path(row["spool_path"]) for row in rows if row["spool_path"]}
                for path in candidates - referenced:
                    if path.is_file() and not path.is_symlink(): path.unlink()
        except Exception:
            # A ledger inspection fault must retain possible committed source data.
            return

    def read_spool(self, item: dict) -> bytes:
        path = Path(item["spool_path"] or "")
        if not path.is_file() or path.is_symlink() or path.parent != self.spool_dir:
            raise LibraryError("recoverable staging content is unavailable", "spool_missing", 409)
        return path.read_bytes()

    def clean_spool(self, item: dict) -> None:
        path = Path(item.get("spool_path") or "")
        if path.parent == self.spool_dir and path.is_file() and not path.is_symlink():
            path.unlink()
        with self.tx() as db:
            db.execute("UPDATE items SET spool_path=NULL, updated_at=? WHERE id=?", (now(), item["id"]))

    def create_batch(self, entries: list[dict], idempotency_key: str | None = None) -> dict:
        if not entries or len(entries) > MAX_BATCH_ITEMS:
            raise LibraryError(f"batch must contain 1-{MAX_BATCH_ITEMS} items")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or len(idempotency_key) > 160):
            raise LibraryError("invalid idempotency key")
        with self.tx() as db:
            if idempotency_key:
                row = db.execute("SELECT id FROM batches WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if row:
                    return self.batch(row["id"], db)
            batch_id, created = uuid.uuid4().hex, now()
            db.execute("INSERT INTO batches(id,idempotency_key,created_at,status) VALUES(?,?,?,?)",
                       (batch_id, idempotency_key, created, "queued"))
            for ordinal, entry in enumerate(entries):
                kind = entry["kind"]
                private = entry.get("locator")
                db.execute("""INSERT INTO items(id,batch_id,ordinal,kind,private_locator,public_locator,spool_path,
                           status,stage,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (uuid.uuid4().hex, batch_id, ordinal, kind, private, safe_locator(private), entry.get("spool_path"),
                            "queued", "ingress", json.dumps(entry.get("metadata", {}), ensure_ascii=False), created, created))
            return self.batch(batch_id, db)

    def _item(self, row: sqlite3.Row) -> dict:
        result = dict(row); result["metadata"] = json.loads(result["metadata"]); return result

    def batch(self, batch_id: str, db: sqlite3.Connection | None = None) -> dict:
        with self.lock:
            conn = db or self.db
            batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not batch: raise LibraryError("batch not found", "not_found", 404)
            rows = conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
            counts = {name: 0 for name in STATUSES}; items = []
            for row in rows:
                item = self._item(row); counts[item["status"]] = counts.get(item["status"], 0) + 1
                items.append({k: item[k] for k in ("id", "kind", "public_locator", "status", "stage", "reason", "document_id")})
            status = "cancelled" if batch["cancelled"] else ("complete" if sum(counts[s] for s in ("queued", "fetching", "enriching", "importing")) == 0 else "running")
            return {"batch_id": batch_id, "status": status, "total": len(rows), "counts": counts, "items": items}

    def get_item(self, item_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row: raise LibraryError("item not found", "not_found", 404)
            return self._item(row)

    def public_item(self, item_id: str) -> dict:
        item = self.get_item(item_id)
        return {key: item[key] for key in ("id", "kind", "public_locator", "status", "stage", "reason", "document_id")}

    def acquire_worker(self, owner: str, seconds: float = 30.0) -> bool:
        with self.tx() as db:
            row = db.execute("SELECT owner,until FROM worker_lease WHERE name='worker'").fetchone()
            if row and row["until"] > time.time() and row["owner"] != owner: return False
            db.execute("INSERT INTO worker_lease(name,owner,until) VALUES('worker',?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,until=excluded.until", (owner, time.time() + seconds))
            return True

    def claim(self, owner: str, seconds: float = 300.0) -> dict | None:
        with self.tx() as db:
            current = time.time()
            row = db.execute("""SELECT i.* FROM items i JOIN batches b ON b.id=i.batch_id
                              WHERE b.cancelled=0 AND (
                                (i.status='queued' AND i.next_attempt_at<=?) OR
                                (i.status IN ('fetching','importing') AND i.next_attempt_at<=? AND (i.lease_until IS NULL OR i.lease_until<?))
                              )
                              ORDER BY i.created_at,i.ordinal LIMIT 1""", (current, current, current)).fetchone()
            if not row: return None
            task = row["task_uuid"]
            db.execute("UPDATE items SET status=?,stage=?,lease_owner=?,lease_until=?,attempts=attempts+?,updated_at=? WHERE id=?",
                       ("importing" if task else "fetching", "task_pending" if task else "fetch", owner, current + seconds, 0 if task else 1, now(), row["id"]))
            return self.get_item(row["id"])

    def renew(self, owner: str, item_id: str | None = None, seconds: float = 300.0) -> bool:
        """Renew both leases before bounded external work; ownership is checked."""
        with self.tx() as db:
            current = time.time()
            worker = db.execute("UPDATE worker_lease SET until=? WHERE name='worker' AND owner=?", (current + seconds, owner)).rowcount
            item = 1
            if item_id:
                item = db.execute("UPDATE items SET lease_until=? WHERE id=? AND lease_owner=?", (current + seconds, item_id, owner)).rowcount
            return bool(worker and item)

    def transition(self, item_id: str, status: str, stage: str, *, reason: str | None = None, **values) -> dict:
        if status not in STATUSES: raise LibraryError("invalid item status")
        columns, params = ["status=?", "stage=?", "reason=?", "lease_owner=NULL", "lease_until=NULL", "updated_at=?"], [status, stage, safe_error(reason) if reason else None, now()]
        for key in ("document_id", "task_uuid", "source_id", "canonical_url", "content_hash", "metadata", "next_attempt_at"):
            if key in values:
                columns.append(key + "=?"); value = values[key]
                params.append(json.dumps(value, ensure_ascii=False) if key == "metadata" else value)
        params.append(item_id)
        with self.tx() as db: db.execute("UPDATE items SET " + ",".join(columns) + " WHERE id=?", params)
        return self.get_item(item_id)

    def cancel(self, batch_id: str) -> dict:
        with self.tx() as db:
            if not db.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone(): raise LibraryError("batch not found", "not_found", 404)
            db.execute("UPDATE batches SET cancelled=1 WHERE id=?", (batch_id,))
            db.execute("UPDATE items SET status='cancelled',stage='cancelled',updated_at=? WHERE batch_id=? AND status IN ('queued','fetching','enriching','importing')", (now(), batch_id))
            return self.batch(batch_id, db)

    def resume(self, item_id: str, replacement: dict | None = None) -> dict:
        item = self.get_item(item_id)
        if item["status"] in {"confirmed", "duplicate"}: raise LibraryError("confirmed item cannot be resumed", "resume_conflict", 409)
        if item.get("metadata", {}).get("upload_intent") and not item.get("task_uuid"):
            raise LibraryError("uncertain upload must be reconciled", "resume_conflict", 409)
        values = ["status='queued'", "stage='resume'", "reason=NULL", "lease_owner=NULL", "lease_until=NULL", "next_attempt_at=0", "updated_at=?"]
        params: list[object] = [now()]
        if replacement and "spool_path" in replacement: values.append("spool_path=?"); params.append(replacement["spool_path"])
        if replacement and "metadata" in replacement: values.append("metadata=?"); params.append(json.dumps(replacement["metadata"], ensure_ascii=False))
        params.append(item_id)
        with self.tx() as db: db.execute("UPDATE items SET " + ",".join(values) + " WHERE id=?", params)
        return self.get_item(item_id)

    def enqueue_reanalysis(self, item_id: str, version: str) -> dict:
        with self.tx() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row: raise LibraryError("item not found", "not_found", 404)
            item = self._item(row); metadata = item["metadata"]
            if item["status"] in {"queued", "enriching"} and metadata.get("reanalyze_version") == version: return item
            if item["status"] != "confirmed" or not item.get("document_id"): raise LibraryError("only confirmed documents can be reanalyzed", "reanalyze_conflict", 409)
            metadata = {**metadata, "reanalyze": True, "reanalyze_version": version}
            db.execute("UPDATE items SET status='queued',stage='reanalyze',reason=NULL,next_attempt_at=0,lease_owner=NULL,lease_until=NULL,metadata=?,updated_at=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), now(), item_id))
        return self.get_item(item_id)
