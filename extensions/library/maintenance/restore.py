"""Owner-invoked, isolated Paperless 3.2.1 restore drill.

The drill imports a verified native directory export into a fresh SQLite data
volume. It never connects to the production database or uses production mounts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from . import NATIVE_IMAGE, NATIVE_VERSION, RECEIPT_VERSION
from .backup import (MARKER, RECEIPT, MaintenanceError, _ledger_counts, _regular_tree,
                     _run, _copy_reference, hash_file, load_config, private_json, read_json,
                     relative_file, root_lock, safe_existing, utcnow, verify_export)

COMPARE_MODELS = {
    "documents.document", "documents.customfield", "documents.customfieldinstance",
    "documents.correspondent", "documents.documenttype", "documents.storagepath",
    "documents.tag", "auth.user", "auth.group", "guardian.userobjectpermission",
    "guardian.groupobjectpermission",
}


def _canonical(record: dict) -> dict:
    fields = dict(record["fields"])
    if record["model"] == "documents.document":
        fields.pop("modified", None)  # auto_now on a fresh import
        fields.pop("content", None)  # checked independently as SHA-256
    for key, value in fields.items():
        if isinstance(value, list) and all(isinstance(x, int) for x in value):
            fields[key] = sorted(value)  # M2M retrieval order is not contractual
    return {"model": record["model"], "pk": record["pk"], "fields": fields}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def expected_orm(export: Path) -> dict:
    records = read_json(relative_file(export, "manifest.json"))
    expected: dict[str, dict[str, dict]] = {model: {} for model in COMPARE_MODELS}
    for record in records:
        model = record["model"]
        if model not in COMPARE_MODELS:
            continue
        if not isinstance(record.get("fields"), dict) or not isinstance(record.get("pk"), int):
            raise MaintenanceError("unsupported comparison record")
        key = str(record["pk"])
        if key in expected[model]:
            raise MaintenanceError("duplicate comparison identity")
        value = {"fields_sha256": _digest(_canonical(record))}
        if model == "documents.document":
            content = record["fields"].get("content")
            if not isinstance(content, str):
                raise MaintenanceError("unsupported document content")
            value["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            value["original_sha256"] = hash_file(relative_file(export, record["__exported_file_name__"]))["sha256"]
        expected[model][key] = value
    return expected


# Source and destination use the same canonical form. Only hashes cross the
# stdin boundary; no exported titles, users, fields, or secret values are logged.
ORM_CHECK = r'''
import hashlib, json, os, sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "paperless.settings")
import django
django.setup()
from django.apps import apps
from django.core import serializers
from django.core.serializers.json import DjangoJSONEncoder
from documents.models import Document, CustomFieldInstance
expected = json.load(sys.stdin)
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
def canonical(record):
    fields = dict(record["fields"])
    if record["model"] == "documents.document":
        fields.pop("modified", None)
        fields.pop("content", None)
    for key, value in fields.items():
        if isinstance(value, list) and all(isinstance(x, int) for x in value):
            fields[key] = sorted(value)
    return {"model": record["model"], "pk": record["pk"], "fields": fields}
try:
    total = 0
    for label, source in expected.items():
        Model = apps.get_model(label)
        manager = Model.global_objects if label in ("documents.document", "documents.customfieldinstance") else Model.objects
        query = manager.all()
        if label == "auth.user":
            query = query.exclude(username__in=["consumer", "AnonymousUser"])
        seen = set()
        for obj in query.iterator():
            key = str(obj.pk)
            if key not in source or key in seen:
                raise ValueError("identity mismatch")
            seen.add(key)
            record = serializers.serialize("python", [obj])[0]
            record = json.loads(json.dumps(record, cls=DjangoJSONEncoder))
            if digest(canonical(record)) != source[key]["fields_sha256"]:
                raise ValueError("field mismatch")
            if label == "documents.document":
                if hashlib.sha256(obj.content.encode("utf-8")).hexdigest() != source[key]["content_sha256"]:
                    raise ValueError("content mismatch")
                with obj.source_path.open("rb") as stream:
                    h = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1048576), b""):
                        h.update(chunk)
                if h.hexdigest() != source[key]["original_sha256"]:
                    raise ValueError("original mismatch")
                total += 1
        if seen != set(source):
            raise ValueError("count mismatch")
    print(json.dumps({"ok": True, "documents": total}))
except Exception:
    print(json.dumps({"ok": False}))
    sys.exit(1)
'''


def preflight(root: Path, receipt_path: Path) -> tuple[Path, dict]:
    if receipt_path.name != RECEIPT or receipt_path.parent.parent != root:
        raise MaintenanceError("receipt must name one owned snapshot")
    snapshot = safe_existing(receipt_path.parent, directory=True, mode=0o700)
    marker = read_json(safe_existing(snapshot / MARKER, mode=0o600))
    receipt = read_json(safe_existing(receipt_path, mode=0o600))
    if not isinstance(marker, dict) or marker.get("kind") != "paperless-library-snapshot" or marker.get("id") != snapshot.name or marker.get("revision") != RECEIPT_VERSION:
        raise MaintenanceError("snapshot marker invalid")
    if not isinstance(receipt, dict) or receipt.get("schema_version") != RECEIPT_VERSION or receipt.get("id") != snapshot.name or receipt.get("status") != "success" or receipt.get("kind") != "snapshot" or receipt.get("native_version") != NATIVE_VERSION or receipt.get("native_image") != NATIVE_IMAGE or receipt.get("method") != "native_directory_export_and_sqlite_online_backup" or receipt.get("offsite_verified") is not False or receipt.get("native_container_id") != marker.get("native_container_id"):
        raise MaintenanceError("successful same-version receipt required")
    files = receipt.get("files")
    if not isinstance(files, dict) or set(files) != {"export", "private", "inventory"}:
        raise MaintenanceError("snapshot inventory missing")
    inventory_path = safe_existing(snapshot / "private" / "inventory.json", mode=0o600)
    if hash_file(inventory_path) != files["inventory"]:
        raise MaintenanceError("private inventory hash mismatch")
    inventory = read_json(inventory_path)
    if not isinstance(inventory, dict) or set(inventory) != {"export", "private"}:
        raise MaintenanceError("private inventory invalid")
    for section in ("export", "private"):
        directory = safe_existing(snapshot / section, directory=True, mode=0o700)
        expected = inventory[section]
        actual = set(_regular_tree(directory, require_private=True))
        if section == "private":
            actual.discard("inventory.json")
        if not isinstance(expected, dict) or actual != set(expected):
            raise MaintenanceError("snapshot inventory mismatch")
        for name, details in expected.items():
            if not isinstance(details, dict) or set(details) != {"size", "sha256"}:
                raise MaintenanceError("invalid inventory entry")
            if hash_file(relative_file(directory, name)) != details:
                raise MaintenanceError("snapshot hash mismatch")
        if files[section] != {"count": len(expected) + (1 if section == "private" else 0),
                              "size": sum(d["size"] for d in expected.values()) + (files["inventory"]["size"] if section == "private" else 0)}:
            raise MaintenanceError("snapshot inventory totals mismatch")
    if set(_regular_tree(snapshot)) != ({MARKER, RECEIPT, "private/inventory.json"} | {"export/" + n for n in inventory["export"]} | {"private/" + n for n in inventory["private"]}):
        raise MaintenanceError("unexpected snapshot file")
    export_files, export_counts = verify_export(snapshot / "export")
    if export_files != inventory["export"] or any(receipt["counts"].get(k) != v for k, v in export_counts.items()):
        raise MaintenanceError("native export count mismatch")
    return snapshot, receipt


def verify_ledger_copy(snapshot: Path, drill_dir: Path, counts: dict) -> dict:
    source = snapshot / "private" / "jobs.sqlite"
    target = drill_dir / "jobs.sqlite"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst, pages=128, sleep=0.05)
    os.chmod(target, 0o600)
    actual, refs = _ledger_counts(target)
    mapping = read_json(snapshot / "private" / "reference-map.json")
    if not isinstance(mapping, list) or len(mapping) != len(refs):
        raise MaintenanceError("private reference map mismatch")
    inventory = read_json(snapshot / "private" / "inventory.json")
    copied = {}
    rewrites = []
    for ref, entry in zip(refs, mapping):
        if not isinstance(entry, dict) or set(entry) != {"item_id", "field", "source", "category", "copy"} or any(entry[key] != ref[key] for key in ref):
            raise MaintenanceError("private reference map mismatch")
        relative = entry["copy"]
        prefix = "spools/" if ref["category"] == "spool" else "sources/"
        if not isinstance(relative, str) or not relative.startswith(prefix) or "/" in relative[len(prefix):]:
            raise MaintenanceError("private reference map invalid")
        if relative in copied and copied[relative] != (ref["category"], ref["source"]):
            raise MaintenanceError("private reference collision")
        copied[relative] = (ref["category"], ref["source"])
        original = relative_file(snapshot / "private", relative)
        details = inventory["private"].get(relative)
        if hash_file(original) != details:
            raise MaintenanceError("recovery reference hash mismatch")
        destination = drill_dir / ("staging" if ref["category"] == "spool" else "sources") / Path(relative).name
        rewrites.append((ref, relative, original, destination, details))
    if set(copied) != {name for name in inventory["private"] if name.startswith(("spools/", "sources/"))}:
        raise MaintenanceError("private reference inventory mismatch")
    actual["spool_files"] = sum(name.startswith("spools/") for name in copied)
    actual["file_files"] = sum(name.startswith("sources/") for name in copied)
    keys = ("batches", "items", "spool_references", "spool_files", "file_references", "file_files")
    if not isinstance(counts.get("counts"), dict) or any(actual.get(k) != counts["counts"].get(k) for k in keys):
        raise MaintenanceError("ledger count mismatch")
    (drill_dir / "staging").mkdir(mode=0o700)
    (drill_dir / "sources").mkdir(mode=0o700)
    for _, relative, original, destination, details in rewrites:
        if not destination.exists():
            if _copy_reference(original, destination) != details:
                raise MaintenanceError("drill reference copy mismatch")
        elif hash_file(destination) != details:
            raise MaintenanceError("drill reference hash mismatch")
    with sqlite3.connect(target) as db:
        for ref, _, _, destination, _ in rewrites:
            if ref["field"] in {"spool_path", "private_locator"}:
                changed = db.execute("UPDATE items SET " + ref["field"] + "=? WHERE id=? AND " + ref["field"] + "=?",
                                     (str(destination), ref["item_id"], ref["source"])).rowcount
            else:
                row = db.execute("SELECT metadata FROM items WHERE id=?", (ref["item_id"],)).fetchone()
                if row is None:
                    raise MaintenanceError("drill item missing")
                metadata = json.loads(row[0])
                key = ref["field"].removeprefix("metadata.")
                if key not in {"ocr_spool_path", "supplement_file_path"} or metadata.get(key) != ref["source"]:
                    raise MaintenanceError("drill metadata reference mismatch")
                metadata[key] = str(destination)
                changed = db.execute("UPDATE items SET metadata=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), ref["item_id"])).rowcount
            if changed != 1:
                raise MaintenanceError("drill reference rewrite failed")
    rewritten, new_refs = _ledger_counts(target)
    if any(rewritten.get(k) != actual[k] for k in ("batches", "items", "spool_references", "file_references")) or len(new_refs) != len(rewrites):
        raise MaintenanceError("drill ledger counts changed")
    for new, (_, _, _, destination, details) in zip(new_refs, rewrites):
        if new["source"] != str(destination) or hash_file(destination) != details:
            raise MaintenanceError("drill reference verification failed")
    return actual


class RetainedDrillError(MaintenanceError):
    """Safe owner identifiers for resources retained by a failed drill."""

    def __init__(self, drill_id: str, resources: list[tuple[str, str]]):
        super().__init__("isolated drill failed; resources retained")
        self.drill_id = drill_id
        self.resources = [(kind, identifier[:12] if kind in {"container", "network"} else identifier)
                          for kind, identifier in resources]


def _docker_id(output: bytes) -> str:
    value = output.decode("ascii", "strict").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MaintenanceError("Docker identifier invalid")
    return value


def _wait_broker(name: str, runner) -> None:
    for _ in range(20):
        try:
            if runner(["docker", "exec", name, "redis-cli", "ping"], timeout=5).strip() == b"PONG":
                return
        except MaintenanceError:
            pass
        time.sleep(1)
    raise MaintenanceError("isolated broker unavailable")


def _owned(resource: str, identifier: str, label: str, runner) -> None:
    value = runner(["docker", resource, "inspect", "--format", "{{index .Labels \"paperless.library.drill\"}}", identifier], timeout=30).decode().strip()
    if value != label:
        raise MaintenanceError("drill resource ownership mismatch")


def _cleanup(resources: list[tuple[str, str]], label: str, runner) -> bool:
    # Cleanup only exact IDs created by this invocation, in dependency order.
    try:
        for kind, identifier in resources:
            if kind == "container":
                value = runner(["docker", "inspect", "--format", "{{index .Config.Labels \"paperless.library.drill\"}}", identifier], timeout=30).decode().strip()
                if value != label:
                    raise MaintenanceError("drill resource ownership mismatch")
                runner(["docker", "rm", "-f", identifier], timeout=60)
            elif kind == "volume":
                _owned("volume", identifier, label, runner)
                runner(["docker", "volume", "rm", identifier], timeout=60)
            elif kind == "network":
                _owned("network", identifier, label, runner)
                runner(["docker", "network", "rm", identifier], timeout=60)
        return True
    except MaintenanceError:
        return False


def drill(config_path: str, receipt_path: str, *, runner=_run) -> dict:
    config = load_config(config_path)
    root = config["root"]
    path = Path(receipt_path)
    if not path.is_absolute() or any(p in {".", ".."} for p in receipt_path.split("/")):
        raise MaintenanceError("absolute receipt path required")
    with root_lock(root):
        snapshot, source_receipt = preflight(root, path)
        label = uuid.uuid4().hex
        drill_dir = root / ("drill-" + label)
        drill_dir.mkdir(mode=0o700)
        result = {"schema_version": RECEIPT_VERSION, "kind": "restore_drill", "status": "in_progress",
                  "id": "drill-" + label, "source_snapshot": snapshot.name, "created_at": utcnow(),
                  "completed_at": None, "native_version": NATIVE_VERSION, "native_image": NATIVE_IMAGE,
                  "method": "isolated_native_directory_import_sqlite", "counts": None,
                  "files": None, "limitations": ["same_host_drill", "no_production_restore_performed"],
                  "offsite_verified": False, "cleanup": "pending"}
        private_json(drill_dir / RECEIPT, result)
        resources: list[tuple[str, str]] = []
        try:
            queue_counts = verify_ledger_copy(snapshot, drill_dir, source_receipt)
            expected = expected_orm(snapshot / "export")
            # A local tag can be retargeted. Require its current image ID to
            # match the running production container ID recorded at snapshot.
            image_id = _docker_id(runner(["docker", "image", "inspect", "--format", "{{.Id}}", NATIVE_IMAGE], timeout=30).replace(b"sha256:", b""))
            live_container = _docker_id(runner(["docker", "inspect", "--format", "{{.Id}}", config["container"]], timeout=30))
            if live_container != source_receipt["native_container_id"]:
                raise MaintenanceError("native container identity changed")
            native_id = _docker_id(runner(["docker", "inspect", "--format", "{{.Image}}", config["container"]], timeout=30).replace(b"sha256:", b""))
            if image_id != native_id:
                raise MaintenanceError("native image digest mismatch")
            prefix = "drill-" + label[:16]
            network, data, media, broker, native = (prefix + suffix for suffix in ("-net", "-data", "-media", "-broker", "-paperless"))
            docker_label = "paperless.library.drill=" + label
            network_id = _docker_id(runner(["docker", "network", "create", "--internal", "--driver", "bridge", "--label", docker_label, network], timeout=30))
            resources.insert(0, ("network", network_id))
            private_json(drill_dir / "resources.json", resources)
            for volume in (data, media):
                created = runner(["docker", "volume", "create", "--label", docker_label, volume], timeout=30).decode().strip()
                if created != volume:
                    raise MaintenanceError("Docker volume identity invalid")
                resources.insert(0, ("volume", volume))
                private_json(drill_dir / "resources.json", resources)
            broker_id = _docker_id(runner(["docker", "run", "-d", "--name", broker, "--network", network,
                                            "--label", docker_label, "--entrypoint", "redis-server",
                                            "docker.io/valkey/valkey:9-alpine", "--save", "", "--appendonly", "no"], timeout=60))
            resources.insert(0, ("container", broker_id))
            private_json(drill_dir / "resources.json", resources)
            _wait_broker(broker, runner)
            native_id = _docker_id(runner(["docker", "run", "-d", "--name", native, "--network", network,
                                           "--label", docker_label, "--mount", "type=volume,src=" + data + ",dst=/usr/src/paperless/data",
                                           "--mount", "type=volume,src=" + media + ",dst=/usr/src/paperless/media",
                                           "--mount", "type=bind,src=" + str(snapshot / "export") + ",dst=/snapshot,readonly",
                                           "-e", "PAPERLESS_DBENGINE=sqlite", "-e", "PAPERLESS_REDIS=redis://" + broker + ":6379",
                                           "-e", "PAPERLESS_SECRET_KEY=" + secrets.token_urlsafe(48),
                                           "--entrypoint", "python3", NATIVE_IMAGE, "-c", "import time; time.sleep(7200)"], timeout=60))
            resources.insert(0, ("container", native_id))
            private_json(drill_dir / "resources.json", resources)
            version = runner(["docker", "exec", "-w", "/usr/src/paperless/src", native,
                              "python3", "-c", "from paperless.version import __full_version_str__; print(__full_version_str__)"], timeout=30).decode().strip()
            if version != NATIVE_VERSION:
                raise MaintenanceError("drill image version mismatch")
            runner(["docker", "exec", "-w", "/usr/src/paperless/src", native,
                    "python3", "manage.py", "migrate", "--no-input"], timeout=1200)
            runner(["docker", "exec", "-w", "/usr/src/paperless/src", native,
                    "python3", "manage.py", "document_importer", "/snapshot", "--no-progress-bar"], timeout=7200)
            reply = runner(["docker", "exec", "-i", "-w", "/usr/src/paperless/src", native,
                            "python3", "-c", ORM_CHECK], stdin=json.dumps(expected).encode(), timeout=1800)
            try:
                check = json.loads(reply)
            except (ValueError, TypeError):
                raise MaintenanceError("ORM verification invalid") from None
            if check != {"ok": True, "documents": source_receipt["counts"]["documents"]}:
                raise MaintenanceError("ORM verification mismatch")
            result.update(status="success", completed_at=utcnow(),
                          counts={"documents": check["documents"], **queue_counts},
                          files={"source_receipt": hash_file(path)}, cleanup="pending")
            private_json(drill_dir / RECEIPT, result)
            result["cleanup"] = "complete" if _cleanup(resources, label, runner) else "pending"
            private_json(drill_dir / RECEIPT, result)
            if result["cleanup"] == "complete":
                try:
                    safe_existing(drill_dir / "jobs.sqlite", mode=0o600).unlink()
                    safe_existing(drill_dir / "resources.json", mode=0o600).unlink()
                except (MaintenanceError, OSError):
                    result["cleanup"] = "pending"
                    private_json(drill_dir / RECEIPT, result)
            return result
        except BaseException:
            result.update(status="failed", completed_at=utcnow(), cleanup="retained")
            private_json(drill_dir / RECEIPT, result)
            raise RetainedDrillError(result["id"], resources) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated Paperless restore drill")
    parser.add_argument("--config", required=True, help="absolute path to private maintenance JSON")
    parser.add_argument("--receipt", required=True, help="absolute path to a successful snapshot receipt")
    args = parser.parse_args(argv)
    try:
        result = drill(args.config, args.receipt)
    except Exception as exc:
        if isinstance(exc, RetainedDrillError):
            print(json.dumps({"id": exc.drill_id,
                              "resources": [{"kind": kind, "id": identifier} for kind, identifier in exc.resources]},
                             sort_keys=True), file=sys.stderr)
        else:
            print("restore drill failed: " + (str(exc) if isinstance(exc, MaintenanceError) else "internal error"), file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "id": result["id"], "counts": result["counts"],
                      "cleanup": result["cleanup"], "offsite_verified": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
