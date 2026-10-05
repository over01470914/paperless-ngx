"""Protected directory exports and a consistent Library SQLite backup.

No external configuration is read until the owner invokes the CLI. Subprocess
output is deliberately discarded: native progress and exceptions may disclose
document names, paths, or credentials.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import NATIVE_IMAGE, NATIVE_VERSION, RECEIPT_VERSION
from ..sources import DENY_FILE_WORDS

MARKER = ".backup-owner.json"
RECEIPT = "receipt.json"
PENDING = {"queued", "fetching", "enriching", "importing", "needs_login", "needs_ocr"}
RECOVERABLE = PENDING | {"blocked", "failed", "partial", "cancelled"}
FILE_SUFFIXES = {".txt", ".md", ".pdf", ".docx"}
MAX_REFERENCE_BYTES = 2 * 1024 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class MaintenanceError(RuntimeError):
    """Sanitized, operator-facing failure."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _absolute(value: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise MaintenanceError("absolute path required")
    path = Path(value)
    if any(part in {".", ".."} for part in value.split("/")):
        raise MaintenanceError("unsafe path component")
    if str(path) != value:
        raise MaintenanceError("canonical absolute path required")
    return path


def safe_existing(path: Path, *, directory: bool = False, mode: int | None = None) -> Path:
    """lstat every ancestor, including the final component; never follow links."""
    if not path.is_absolute():
        raise MaintenanceError("absolute path required")
    current = Path("/")
    for part in path.parts[1:]:
        if part in {".", ".."}:
            raise MaintenanceError("unsafe path component")
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise MaintenanceError("required path is unavailable") from None
        if stat.S_ISLNK(info.st_mode):
            raise MaintenanceError("symlink is forbidden")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise MaintenanceError("unsafe ancestor")
    info = path.lstat()
    if directory and not stat.S_ISDIR(info.st_mode):
        raise MaintenanceError("directory required")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise MaintenanceError("regular file required")
    if not directory and info.st_nlink != 1:
        raise MaintenanceError("hardlinked file is forbidden")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise MaintenanceError("private mode required")
    return path


def relative_file(root: Path, value: str) -> Path:
    _safe_reference(value)
    pure = PurePosixPath(value)
    path = root.joinpath(*pure.parts)
    safe_existing(path)
    return path


def _safe_reference(value: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise MaintenanceError("unsafe item reference")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(p in {".", "..", ""} for p in value.split("/")):
        raise MaintenanceError("unsafe item reference")


def hash_file(path: Path) -> dict:
    safe_existing(path)
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return {"size": path.stat().st_size, "sha256": h.hexdigest()}


def private_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def read_json(path: Path) -> object:
    safe_existing(path)
    with path.open("rb") as handle:
        return json.load(handle)


def _run(args: list[str], *, stdin: bytes | None = None, timeout: int = 1800) -> bytes:
    try:
        result = subprocess.run(args, input=stdin, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise MaintenanceError("native operation unavailable or timed out") from None
    if result.returncode:
        raise MaintenanceError("native operation failed")
    return result.stdout


def inspect_native(name: str, export_bind: Path, runner=_run) -> str:
    if not NAME.fullmatch(name):
        raise MaintenanceError("invalid container name")
    # Narrow format avoids requesting Config.Env or printing inspect JSON.
    template = "{{json .Id}}\n{{json .Name}}\n{{json .Config.Image}}\n{{json .State.Running}}\n{{json .Mounts}}"
    try:
        lines = runner(["docker", "inspect", "--format", template, name], timeout=30).decode().splitlines()
        if len(lines) != 5:
            raise ValueError
        identifier, actual_name, image, running, mounts = map(json.loads, lines)
        if actual_name != "/" + name or image != NATIVE_IMAGE or running is not True:
            raise ValueError
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ValueError
        if not any(m.get("Type") == "bind" and m.get("Source") == str(export_bind)
                   and m.get("Destination") == "/usr/src/paperless/export" and m.get("RW") is True
                   for m in mounts):
            raise ValueError
        actual_version = runner(["docker", "exec", "-w", "/usr/src/paperless/src", name,
                                 "python3", "-c", "from paperless.version import __full_version_str__; print(__full_version_str__)"], timeout=30).decode().strip()
        if actual_version != NATIVE_VERSION:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, KeyError):
        raise MaintenanceError("native identity, version, or export mount mismatch") from None
    return identifier


def _regular_tree(root: Path, *, require_private: bool = False) -> list[str]:
    safe_existing(root, directory=True)
    found: list[str] = []
    def visit(directory: Path) -> None:
        for entry in os.scandir(directory):
            path = Path(entry.path)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                if require_private and stat.S_IMODE(info.st_mode) != 0o700:
                    raise MaintenanceError("private directory mode required")
                visit(path)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if require_private and stat.S_IMODE(info.st_mode) != 0o600:
                    raise MaintenanceError("private file mode required")
                found.append(path.relative_to(root).as_posix())
            else:
                raise MaintenanceError("unsafe snapshot entry")
    visit(root)
    return sorted(found)


def _privatize_tree(root: Path) -> None:
    safe_existing(root, directory=True)
    def visit(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
        for entry in os.scandir(directory):
            path = Path(entry.path)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    if os.fstat(fd).st_nlink != 1:
                        raise MaintenanceError("unsafe snapshot entry")
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
            else:
                raise MaintenanceError("unsafe snapshot entry")
    visit(root)


def verify_export(export: Path) -> tuple[dict, dict]:
    """Return private file inventory and public counts. Source records stay private."""
    metadata = read_json(relative_file(export, "metadata.json"))
    records = read_json(relative_file(export, "manifest.json"))
    if not isinstance(metadata, dict) or metadata.get("version") != NATIVE_VERSION or not isinstance(records, list):
        raise MaintenanceError("export version or manifest invalid")
    references: set[str] = {"metadata.json", "manifest.json"}
    docs: set[int] = set()
    bundles = 0
    originals = thumbnails = archives = 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("model"), str) or not isinstance(record.get("fields"), dict):
            raise MaintenanceError("invalid export record")
        if record["model"] == "documents.document":
            pk = record.get("pk")
            if not isinstance(pk, int) or pk in docs:
                raise MaintenanceError("invalid document identity")
            docs.add(pk)
            keys = ("__exported_file_name__", "__exported_thumbnail_name__", "__exported_archive_name__")
            if not record.get(keys[0]) or not record.get(keys[1]):
                raise MaintenanceError("incomplete document export")
            if bool(record["fields"].get("archive_filename")) != bool(record.get(keys[2])):
                raise MaintenanceError("archive reference mismatch")
            for field in ("filename", "archive_filename"):
                stored = record["fields"].get(field)
                if stored is not None:
                    _safe_reference(stored)
            for key in keys:
                if record.get(key):
                    references.add(record[key])
            originals += 1
            thumbnails += 1
            archives += bool(record.get(keys[2]))
        elif record["model"] == "documents.sharelinkbundle" and record.get("__exported_share_link_bundle_name__"):
            _safe_reference(record["fields"].get("file_path"))
            references.add(record["__exported_share_link_bundle_name__"])
            bundles += 1
    if len(references) != 2 + originals + thumbnails + archives + bundles:
        raise MaintenanceError("duplicate native file reference")
    inventory = {}
    for ref in sorted(references):
        inventory[ref] = hash_file(relative_file(export, ref))
    actual = set(_regular_tree(export))
    if actual != references:
        raise MaintenanceError("export contains missing or unexpected files")
    return inventory, {"documents": len(docs), "records": len(records), "bundles": bundles,
                       "originals": originals, "thumbnails": thumbnails, "archives": archives,
                       "export_files": len(inventory), "export_bytes": sum(v["size"] for v in inventory.values())}


def _ledger_counts(path: Path) -> tuple[dict, list[dict]]:
    safe_existing(path)
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise MaintenanceError("ledger integrity check failed")
            batches = db.execute("SELECT count(*) FROM batches").fetchone()[0]
            items = db.execute("SELECT count(*) FROM items").fetchone()[0]
            rows = db.execute("SELECT id,kind,status,private_locator,spool_path,metadata FROM items ORDER BY id").fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        raise MaintenanceError("ledger schema or read failed") from None
    references = []
    for item_id, kind, status, locator, spool, metadata_raw in rows:
        try:
            metadata = json.loads(metadata_raw)
        except (TypeError, ValueError):
            raise MaintenanceError("ledger item metadata invalid") from None
        if not isinstance(metadata, dict):
            raise MaintenanceError("ledger item metadata invalid")
        if kind == "text" and status in PENDING and not spool and not metadata.get("reanalyze_version"):
            raise MaintenanceError("pending text spool missing")
        def add(field: str, source: object, category: str) -> None:
            if not isinstance(source, str) or not source:
                raise MaintenanceError("recovery reference invalid")
            references.append({"item_id": str(item_id), "field": field, "source": source, "category": category})
        if spool is not None:
            add("spool_path", spool, "spool")
        if status in RECOVERABLE and not metadata.get("reanalyze_version"):
            if metadata.get("ocr_spool_path") is not None:
                add("metadata.ocr_spool_path", metadata["ocr_spool_path"], "spool")
            if kind == "file":
                add("private_locator", locator, "file")
            if metadata.get("supplement_file_path") is not None:
                add("metadata.supplement_file_path", metadata["supplement_file_path"], "file")
    return {"batches": batches, "items": items,
            "spool_references": sum(ref["category"] == "spool" for ref in references),
            "file_references": sum(ref["category"] == "file" for ref in references)}, references


def _accepted_roots(values: object) -> list[Path]:
    if not isinstance(values, list) or not values:
        raise MaintenanceError("accepted file roots invalid")
    roots = []
    for value in values:
        root = safe_existing(_absolute(str(value) if isinstance(value, Path) else value), directory=True)
        roots.append(root)
    return roots


def _source_reference(raw: str, category: str, staging: Path, roots: list[Path]) -> Path:
    path = _absolute(raw)
    safe_existing(path)
    if category == "spool":
        if path.parent != staging:
            raise MaintenanceError("spool outside staging")
    else:
        if not any(path.is_relative_to(root) for root in roots):
            raise MaintenanceError("source outside accepted roots")
        if any(word in part.lower() for part in path.parts for word in DENY_FILE_WORDS):
            raise MaintenanceError("sensitive source path")
        if path.suffix.lower() not in FILE_SUFFIXES:
            raise MaintenanceError("unsupported source type")
    return path


def _open_source_nofollow(path: Path) -> int:
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _copy_reference(source: Path, target: Path) -> dict:
    """Copy a stable regular inode through no-follow descriptors with a size ceiling."""
    before = source.lstat()
    if before.st_size > MAX_REFERENCE_BYTES:
        raise MaintenanceError("recovery reference too large")
    try:
        source_fd = _open_source_nofollow(source)
    except OSError:
        raise MaintenanceError("recovery reference unavailable") from None
    target_fd = None
    try:
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise MaintenanceError("recovery reference changed")
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(source_fd, "rb", closefd=False) as src, os.fdopen(target_fd, "wb", closefd=False) as dst:
            while True:
                chunk = src.read(min(1024 * 1024, MAX_REFERENCE_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_REFERENCE_BYTES:
                    raise MaintenanceError("recovery reference too large")
                digest.update(chunk)
                dst.write(chunk)
            dst.flush(); os.fsync(dst.fileno())
        after = os.fstat(source_fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns, 1) or size != opened.st_size:
            raise MaintenanceError("recovery reference changed")
        details = {"size": size, "sha256": digest.hexdigest()}
        os.lseek(source_fd, 0, os.SEEK_SET)
        second = hashlib.sha256()
        with os.fdopen(source_fd, "rb", closefd=False) as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                second.update(chunk)
        if second.hexdigest() != details["sha256"] or os.fstat(source_fd).st_mtime_ns != opened.st_mtime_ns:
            raise MaintenanceError("recovery reference changed")
        if hash_file(target) != details:
            raise MaintenanceError("recovery copy hash mismatch")
        return details
    finally:
        os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)


def backup_ledger(runtime: Path, private: Path, allowed_file_roots: list[str | Path]) -> tuple[dict, dict]:
    staging = safe_existing(runtime / "staging", directory=True)
    roots = _accepted_roots(allowed_file_roots)
    source = safe_existing(runtime / "jobs.sqlite")
    target = private / "jobs.sqlite"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst, pages=128, sleep=0.05)
    finally:
        dst.close(); src.close()
    os.chmod(target, 0o600)
    counts, refs = _ledger_counts(target)
    copied: dict[tuple[str, str], str] = {}
    private_inventory = {"jobs.sqlite": hash_file(target)}
    mapping = []
    for category in ("spool", "file"):
        (private / ("spools" if category == "spool" else "sources")).mkdir(mode=0o700)
    for ref in refs:
        raw, category = ref["source"], ref["category"]
        source_path = _source_reference(raw, category, staging, roots)
        key = (category, raw)
        if key not in copied:
            # Index plus original suffix avoids basename collisions and preserves file type.
            name = f"{len(copied):08x}-{source_path.name}"
            relative = ("spools/" if category == "spool" else "sources/") + name
            private_inventory[relative] = _copy_reference(source_path, private / relative)
            copied[key] = relative
        mapping.append({**ref, "copy": copied[key]})
    counts["spool_files"] = sum(name.startswith("spools/") for name in copied.values())
    counts["file_files"] = sum(name.startswith("sources/") for name in copied.values())
    # Item, field, and original absolute paths remain only in the private map.
    private_json(private / "reference-map.json", mapping)
    private_inventory["reference-map.json"] = hash_file(private / "reference-map.json")
    return counts, private_inventory


def load_config(path: str) -> dict:
    raw = read_json(safe_existing(_absolute(path), mode=0o600))
    if not isinstance(raw, dict):
        raise MaintenanceError("maintenance config invalid")
    try:
        export_bind = safe_existing(_absolute(raw["host_export_bind"]), directory=True)
        root = safe_existing(_absolute(raw["external_root"]), directory=True, mode=0o700)
        library_config = safe_existing(_absolute(raw["library_config"]), mode=0o600)
        container = raw["native_container"]
    except (KeyError, TypeError):
        raise MaintenanceError("maintenance config invalid") from None
    if root.parent != export_bind or not NAME.fullmatch(container):
        raise MaintenanceError("fixed external root or container invalid")
    if root.stat().st_uid != os.getuid():
        raise MaintenanceError("external root ownership mismatch")
    return {"export_bind": export_bind, "root": root, "library_config": library_config, "container": container}


def _library_paths(config_path: Path) -> tuple[Path, list[Path]]:
    config = read_json(config_path)
    if not isinstance(config, dict) or not isinstance(config.get("runtime_dir"), str):
        raise MaintenanceError("Library config invalid")
    runtime = safe_existing(_absolute(str(Path(config["runtime_dir"]).expanduser())), directory=True)
    return runtime, _accepted_roots(config.get("allowed_file_roots"))


@contextlib.contextmanager
def root_lock(root: Path):
    lock = root / ".maintenance.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600 or os.fstat(fd).st_nlink != 1:
            raise MaintenanceError("unsafe lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MaintenanceError("maintenance already running") from None
        yield
    finally:
        os.close(fd)


def _make_snapshot(root: Path, identifier: str) -> Path:
    name = "snapshot-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    snapshot = root / name
    snapshot.mkdir(mode=0o700)
    private_json(snapshot / MARKER, {"kind": "paperless-library-snapshot", "revision": RECEIPT_VERSION,
                                     "id": name, "native_container_id": identifier})
    return snapshot


def snapshot(config_path: str, *, retain: int = 7, runner=_run) -> dict:
    if retain < 1:
        raise MaintenanceError("retention must be positive")
    config = load_config(config_path)
    with root_lock(config["root"]):
        identifier = inspect_native(config["container"], config["export_bind"], runner)
        runtime, allowed_roots = _library_paths(config["library_config"])
        library_config = config["library_config"]
        snap = _make_snapshot(config["root"], identifier)
        receipt = {"schema_version": RECEIPT_VERSION, "kind": "snapshot", "status": "in_progress",
                   "id": snap.name, "created_at": utcnow(), "completed_at": None,
                   "native_version": NATIVE_VERSION, "native_image": NATIVE_IMAGE,
                   "native_container_id": identifier, "method": "native_directory_export_and_sqlite_online_backup",
                   "counts": None, "files": None, "limitations": ["same_host_only", "no_live_restore_performed"],
                   "offsite_verified": False}
        private_json(snap / RECEIPT, receipt)
        try:
            export = snap / "export"
            private = snap / "private"
            export.mkdir(mode=0o700)
            private.mkdir(mode=0o700)
            config_target = private / "library-config.json"
            with library_config.open("rb") as src:
                fd = os.open(config_target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            # The export target already exists inside the validated host bind.
            target = "/usr/src/paperless/export/" + config["root"].name + "/" + snap.name + "/export"
            runner(["docker", "exec", "--user", f"{os.getuid()}:{os.getgid()}",
                    "-w", "/usr/src/paperless/src", config["container"],
                    "python3", "manage.py", "document_exporter", target], timeout=7200)
            # The exporter controls new file modes. Lock them down before parsing.
            _privatize_tree(export)
            inventory, export_counts = verify_export(export)
            ledger_counts, private_inventory = backup_ledger(runtime, private, allowed_roots)
            private_inventory["library-config.json"] = hash_file(config_target)
            private_json(private / "inventory.json", {"export": inventory, "private": private_inventory})
            inventory_receipt = hash_file(private / "inventory.json")
            receipt.update(status="success", completed_at=utcnow(),
                           counts={**export_counts, **ledger_counts},
                           files={"inventory": inventory_receipt,
                                  "export": {"count": len(inventory), "size": export_counts["export_bytes"]},
                                  "private": {"count": len(private_inventory) + 1,
                                              "size": sum(v["size"] for v in private_inventory.values()) + inventory_receipt["size"]}})
            private_json(snap / RECEIPT, receipt)
            prune_successful(config["root"], keep=retain)
            return receipt
        except BaseException:
            receipt.update(status="failed", completed_at=utcnow(), counts=None, files=None)
            private_json(snap / RECEIPT, receipt)
            raise


def _remove_owned(snapshot: Path, root: Path) -> None:
    if snapshot.parent != root or not snapshot.name.startswith("snapshot-"):
        raise MaintenanceError("unsafe retention target")
    marker = read_json(safe_existing(snapshot / MARKER, mode=0o600))
    if not isinstance(marker, dict) or marker.get("kind") != "paperless-library-snapshot" or marker.get("id") != snapshot.name:
        raise MaintenanceError("snapshot ownership mismatch")
    # dir_fd + O_NOFOLLOW ensures a swapped directory link is never traversed.
    def remove_dir(parent_fd: int, name: str) -> None:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            with os.scandir(fd) as entries:
                names = [entry.name for entry in entries]
            for child in names:
                info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    remove_dir(fd, child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    os.unlink(child, dir_fd=fd)
                else:
                    raise MaintenanceError("unsafe retention entry")
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent_fd)
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        remove_dir(parent_fd, snapshot.name)
    finally:
        os.close(parent_fd)


def prune_successful(root: Path, *, keep: int = 7) -> list[str]:
    safe_existing(root, directory=True, mode=0o700)
    if keep < 1:
        raise MaintenanceError("retention must be positive")
    successful = []
    for entry in root.iterdir():
        if not entry.name.startswith("snapshot-"):
            continue
        safe_existing(entry, directory=True)
        marker = read_json(safe_existing(entry / MARKER, mode=0o600))
        receipt = read_json(safe_existing(entry / RECEIPT, mode=0o600))
        if not isinstance(marker, dict) or marker.get("id") != entry.name or marker.get("kind") != "paperless-library-snapshot":
            raise MaintenanceError("snapshot ownership mismatch")
        if not isinstance(receipt, dict) or receipt.get("id") != entry.name:
            raise MaintenanceError("snapshot receipt mismatch")
        if receipt.get("status") == "success":
            completed = receipt.get("completed_at")
            if not isinstance(completed, str):
                raise MaintenanceError("snapshot completion time invalid")
            successful.append((completed, entry))
    successful.sort(key=lambda pair: (pair[0], pair[1].name), reverse=True)
    removed = []
    # Validate every deletion candidate before mutating any candidate.
    from .restore import preflight
    for _, entry in successful[keep:]:
        preflight(root, entry / RECEIPT)
    for _, entry in successful[keep:]:
        _remove_owned(entry, root)
        removed.append(entry.name)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Protected Paperless snapshot")
    parser.add_argument("--config", required=True, help="absolute path to private maintenance JSON")
    parser.add_argument("--retain", type=int, default=7)
    args = parser.parse_args(argv)
    try:
        result = snapshot(args.config, retain=args.retain)
    except Exception as exc:
        print("snapshot failed: " + (str(exc) if isinstance(exc, MaintenanceError) else "internal error"), file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "id": result["id"], "counts": result["counts"],
                      "offsite_verified": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
