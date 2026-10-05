"""Synthetic files and SQLite only. Docker is an in-process fake."""
from __future__ import annotations

import json
import importlib.util
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extensions.library.maintenance import NATIVE_IMAGE
from extensions.library.maintenance import backup, restore


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.export_bind = self.base / "export"
        self.export_bind.mkdir(mode=0o700)
        self.root = self.export_bind / "protected"
        self.root.mkdir(mode=0o700)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.staging = self.runtime / "staging"
        self.staging.mkdir(mode=0o700)
        self.accepted = self.base / "accepted"
        self.accepted.mkdir(mode=0o700)
        self.spool = self.staging / "body.spool"
        self.spool.write_bytes(b"pending text")
        self.ocr = self.staging / "ocr.spool"
        self.ocr.write_bytes(b"[]")
        with sqlite3.connect(self.runtime / "jobs.sqlite") as db:
            db.execute("CREATE TABLE batches (id TEXT)")
            db.execute("CREATE TABLE items (id TEXT,kind TEXT,status TEXT,private_locator TEXT,spool_path TEXT,metadata TEXT)")
            db.execute("INSERT INTO batches VALUES ('b1')")
            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?)", ("i1", "text", "queued", None, str(self.spool), json.dumps({"ocr_spool_path": str(self.ocr)})))
            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?)", ("i2", "text", "confirmed", None, None, json.dumps({"ocr_spool_path": str(self.staging / "old.spool")})))
        self.library_config = self.base / "library.json"
        self.library_config.write_text(json.dumps({"runtime_dir": str(self.runtime), "allowed_file_roots": [str(self.accepted)],
                                                   "writer_token": "synthetic-private-token"}))
        os.chmod(self.library_config, 0o600)
        self.config = self.base / "maintenance.json"
        self.config.write_text(json.dumps({"host_export_bind": str(self.export_bind), "external_root": str(self.root),
                                           "library_config": str(self.library_config), "native_container": "paperless-webserver-1"}))
        os.chmod(self.config, 0o600)

    def make_export(self, export: Path):
        (export / "original.txt").write_bytes(b"original bytes")
        (export / "thumbnail.webp").write_bytes(b"webp bytes")
        (export / "archive.pdf").write_bytes(b"pdf bytes")
        fields = {"title": "Private synthetic title", "content": "private synthetic content", "archive_filename": "archive.pdf",
                  "tags": [2, 1], "owner": 1, "created": "2026-01-01", "mime_type": "text/plain"}
        record = {"model": "documents.document", "pk": 1, "fields": fields,
                  "__exported_file_name__": "original.txt", "__exported_thumbnail_name__": "thumbnail.webp",
                  "__exported_archive_name__": "archive.pdf"}
        (export / "manifest.json").write_text(json.dumps([record]))
        (export / "metadata.json").write_text(json.dumps({"version": "3.2.1"}))

    def fake_snapshot_runner(self, args, **kwargs):
        if args[:3] == ["docker", "inspect", "--format"]:
            mounts = [{"Type": "bind", "Source": str(self.export_bind), "Destination": "/usr/src/paperless/export", "RW": True}]
            return "\n".join(json.dumps(x) for x in ("a" * 64, "/paperless-webserver-1", NATIVE_IMAGE, True, mounts)).encode()
        if "document_exporter" in args:
            self.assertFalse(any(flag in args for flag in ("--delete", "--data-only", "--no-archive", "--no-thumbnail")))
            snapshot = self.root / Path(args[-1]).parent.name
            self.make_export(snapshot / "export")
            return b""
        if args[:2] == ["docker", "exec"] and "__full_version_str__" in args[-1]:
            return b"3.2.1\n"
        raise AssertionError(args)

    def snapshot(self):
        return backup.snapshot(str(self.config), runner=self.fake_snapshot_runner)


class SnapshotTests(Fixture):
    def test_snapshot_private_inventory_and_receipt(self):
        receipt = self.snapshot()
        snapshot = self.root / receipt["id"]
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["counts"]["documents"], 1)
        self.assertEqual(receipt["counts"]["batches"], 1)
        self.assertEqual(receipt["counts"]["items"], 2)
        self.assertEqual(receipt["counts"]["spool_references"], 2)
        schema = json.loads(Path("extensions/library/maintenance/contracts/backup-receipt.schema.json").read_text())
        required = schema["$defs"]["snapshot"]["allOf"][1]["properties"]["counts"]["required"]
        self.assertEqual(set(receipt["counts"]), set(required))
        self.assertFalse(receipt["offsite_verified"])
        self.assertNotIn("synthetic-private-token", json.dumps(receipt))
        self.assertNotIn("Private synthetic title", json.dumps(receipt))
        self.assertNotIn(str(self.staging), json.dumps(receipt))
        self.assertEqual((snapshot / "private" / "library-config.json").read_bytes(), self.library_config.read_bytes())
        mapping = json.loads((snapshot / "private" / "reference-map.json").read_text())
        self.assertEqual((snapshot / "private" / mapping[0]["copy"]).read_bytes(), b"pending text")
        self.assertEqual((snapshot / "private" / mapping[1]["copy"]).read_bytes(), b"[]")
        for name in ("receipt.json", "private/inventory.json", "private/jobs.sqlite", "export/manifest.json"):
            self.assertEqual((snapshot / name).stat().st_mode & 0o777, 0o600)
        restore.preflight(self.root, snapshot / "receipt.json")
        if importlib.util.find_spec("jsonschema"):
            import jsonschema
            schema = json.loads(Path("extensions/library/maintenance/contracts/backup-receipt.schema.json").read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(receipt, schema)
            bad = dict(receipt, offsite_verified=True)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)

    def test_failure_receipt_persists(self):
        def runner(args, **kwargs):
            if "document_exporter" in args:
                raise backup.MaintenanceError("native operation failed")
            return self.fake_snapshot_runner(args, **kwargs)
        with self.assertRaises(backup.MaintenanceError):
            backup.snapshot(str(self.config), runner=runner)
        snapshot = next(p for p in self.root.iterdir() if p.name.startswith("snapshot-"))
        receipt = json.loads((snapshot / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        if importlib.util.find_spec("jsonschema"):
            import jsonschema
            schema = json.loads(Path("extensions/library/maintenance/contracts/backup-receipt.schema.json").read_text())
            jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(receipt)

    def test_symlink_export_and_spool_rejected(self):
        export = self.root / "export-test"
        export.mkdir()
        self.make_export(export)
        (export / "original.txt").unlink()
        (export / "original.txt").symlink_to(self.spool)
        with self.assertRaises(backup.MaintenanceError):
            backup.verify_export(export)
        self.spool.unlink()
        self.spool.symlink_to(self.ocr)
        private = self.root / "private-test"
        private.mkdir(mode=0o700)
        with self.assertRaises(backup.MaintenanceError):
            backup.backup_ledger(self.runtime, private, [self.accepted])

    def test_retention_only_successful_owned_directories(self):
        first = self.snapshot()
        second = self.snapshot()
        failed = self.root / "snapshot-20200101T000000Z-deadbeef0000"
        failed.mkdir(mode=0o700)
        backup.private_json(failed / backup.MARKER, {"kind": "paperless-library-snapshot", "id": failed.name, "revision": 1})
        backup.private_json(failed / backup.RECEIPT, {"id": failed.name, "status": "failed"})
        removed = backup.prune_successful(self.root, keep=1)
        self.assertEqual(removed, [first["id"]])
        self.assertTrue((self.root / second["id"]).exists())
        self.assertTrue(failed.exists())

    def test_preflight_detects_tamper(self):
        receipt = self.snapshot()
        snapshot = self.root / receipt["id"]
        (snapshot / "export" / "original.txt").write_bytes(b"tampered")
        with self.assertRaises(backup.MaintenanceError):
            restore.preflight(self.root, snapshot / "receipt.json")

    def test_manifest_storage_traversal_rejected(self):
        export = self.root / "export-test"
        export.mkdir()
        self.make_export(export)
        manifest = json.loads((export / "manifest.json").read_text())
        manifest[0]["fields"]["filename"] = "../outside.txt"
        (export / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(backup.MaintenanceError):
            backup.verify_export(export)

    def test_reanalysis_with_no_live_spool(self):
        with sqlite3.connect(self.runtime / "jobs.sqlite") as db:
            db.execute("UPDATE items SET status='queued',metadata=? WHERE id='i2'",
                       (json.dumps({"reanalyze_version": "v2", "ocr_spool_path": str(self.staging / "old.spool")}),))
        private = self.root / "private-test"
        private.mkdir(mode=0o700)
        counts, _ = backup.backup_ledger(self.runtime, private, [self.accepted])
        self.assertEqual(counts["spool_references"], 2)

    def test_recovery_files_pending_blocked_ocr_supplement_and_collision(self):
        left = self.accepted / "left"
        right = self.accepted / "right"
        left.mkdir(); right.mkdir()
        first, second = left / "same.pdf", right / "same.pdf"
        first.write_bytes(b"first PDF")
        second.write_bytes(b"second PDF")
        with sqlite3.connect(self.runtime / "jobs.sqlite") as db:
            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?)", ("i3", "file", "needs_ocr", str(first), None, "{}"))
            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?)", ("i4", "file", "blocked", str(second), None,
                       json.dumps({"supplement_file_path": str(first), "ocr_spool_path": str(self.ocr)})))
            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?)", ("i5", "url", "blocked", "https://example.invalid/note", None, "{}"))
        receipt = self.snapshot()
        self.assertEqual(receipt["counts"]["file_references"], 3)
        self.assertEqual(receipt["counts"]["file_files"], 2)
        self.assertEqual(receipt["counts"]["spool_references"], 3)
        snap = self.root / receipt["id"]
        mapping = json.loads((snap / "private" / "reference-map.json").read_text())
        self.assertEqual(len({m["copy"] for m in mapping if m["category"] == "file"}), 2)
        self.assertNotIn(str(first), json.dumps(receipt))
        # A drill must work from the protected bytes, even after accepted-root
        # source files disappear; the original paths are mapping keys only.
        first.unlink()
        second.unlink()
        drill = self.root / "local-check"
        drill.mkdir(mode=0o700)
        counts = restore.verify_ledger_copy(snap, drill, receipt)
        self.assertEqual(counts["file_files"], 2)
        with sqlite3.connect(drill / "jobs.sqlite") as db:
            rows = db.execute("SELECT id,private_locator,spool_path,metadata FROM items ORDER BY id").fetchall()
        for item_id, locator, spool, raw in rows:
            metadata = json.loads(raw)
            for value in (locator if item_id in {"i3", "i4"} else None, spool,
                          metadata.get("supplement_file_path"), metadata.get("ocr_spool_path") if item_id != "i2" else None):
                if value:
                    self.assertTrue(Path(value).is_relative_to(drill))
                    self.assertTrue(Path(value).is_file())
        self.assertEqual((drill / "sources" / next(m["copy"].split("/", 1)[1] for m in mapping if m["source"] == str(first))).read_bytes(), b"first PDF")
        self.assertEqual(restore.preflight(self.root, snap / "receipt.json")[1]["counts"]["file_files"], 2)

    def test_required_source_references_fail_closed(self):
        source = self.accepted / "document.txt"
        source.write_bytes(b"source")
        with sqlite3.connect(self.runtime / "jobs.sqlite") as db:
            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?)", ("i3", "file", "blocked", str(source), None, "{}"))
        linked_dir = self.accepted / "linked"
        linked_dir.symlink_to(self.base)
        cases = [self.accepted / "missing.txt", self.base / "outside.txt", self.accepted / "private-key.txt",
                 self.accepted / "unsupported.bin", self.accepted / "link.txt", self.accepted / "hard.txt",
                 linked_dir / "outside.txt", self.accepted / ".." / "accepted" / "document.txt"]
        cases[1].write_bytes(b"outside")
        cases[2].write_bytes(b"sensitive")
        cases[3].write_bytes(b"unsupported")
        cases[4].symlink_to(source)
        os.link(source, cases[5])
        for index, value in enumerate(cases):
            with self.subTest(case=value.name):
                with sqlite3.connect(self.runtime / "jobs.sqlite") as db:
                    db.execute("UPDATE items SET private_locator=? WHERE id='i3'", (str(value),))
                private = self.root / f"private-test-{index}"
                private.mkdir(mode=0o700)
                with self.assertRaises(backup.MaintenanceError):
                    backup.backup_ledger(self.runtime, private, [self.accepted])
        with sqlite3.connect(self.runtime / "jobs.sqlite") as db:
            db.execute("UPDATE items SET private_locator=? WHERE id='i3'", (str(source),))
        private = self.root / "private-test-denied"
        private.mkdir(mode=0o700)
        real_open = os.open
        def denied(path, *args, **kwargs):
            if path == source.name and kwargs.get("dir_fd") is not None:
                raise PermissionError("synthetic denied source")
            return real_open(path, *args, **kwargs)
        with patch.object(backup.os, "open", side_effect=denied):
            with self.assertRaises(backup.MaintenanceError):
                backup.backup_ledger(self.runtime, private, [self.accepted])

    def test_reference_map_and_hash_tamper_rejected(self):
        source = self.accepted / "document.txt"
        source.write_bytes(b"source")
        with sqlite3.connect(self.runtime / "jobs.sqlite") as db:
            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?)", ("i3", "file", "blocked", str(source), None, "{}"))
        receipt = self.snapshot()
        snap = self.root / receipt["id"]
        mapping = json.loads((snap / "private" / "reference-map.json").read_text())
        mapping[-1]["item_id"] = "wrong"
        backup.private_json(snap / "private" / "reference-map.json", mapping)
        (self.root / "drill-check").mkdir(mode=0o700)
        with self.assertRaises(backup.MaintenanceError):
            restore.verify_ledger_copy(snap, self.root / "drill-check", receipt)
        source_copy = snap / "private" / mapping[-1]["copy"]
        source_copy.write_bytes(b"tampered")
        with self.assertRaises(backup.MaintenanceError):
            restore.preflight(self.root, snap / "receipt.json")

    def test_missing_pending_source_keeps_failed_snapshot(self):
        missing = self.accepted / "pending.pdf"
        with sqlite3.connect(self.runtime / "jobs.sqlite") as db:
            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?)", ("i3", "file", "needs_ocr", str(missing), None, "{}"))
        with self.assertRaises(backup.MaintenanceError):
            self.snapshot()
        snap = next(path for path in self.root.iterdir() if path.name.startswith("snapshot-"))
        receipt = json.loads((snap / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertIsNone(receipt["counts"])
        self.assertNotIn(str(missing), json.dumps(receipt))


class DrillTests(Fixture):
    def test_mocked_isolated_drill(self):
        source = self.snapshot()
        commands = []
        label = None
        def runner(args, **kwargs):
            nonlocal label
            commands.append(args)
            if args[:3] == ["docker", "image", "inspect"]:
                return ("sha256:" + "b" * 64).encode()
            if args[:3] == ["docker", "inspect", "--format"] and args[3] == "{{.Id}}":
                return ("a" * 64).encode()
            if args[:3] == ["docker", "inspect", "--format"] and args[3] == "{{.Image}}":
                return ("sha256:" + "b" * 64).encode()
            if args[:3] == ["docker", "network", "create"]:
                label = args[args.index("--label") + 1].split("=", 1)[1]
                return ("c" * 64).encode()
            if args[:3] == ["docker", "volume", "create"]:
                return args[-1].encode()
            if args[:3] == ["docker", "run", "-d"]:
                return (("d" if "redis-server" in args else "e") * 64).encode()
            if "redis-cli" in args:
                return b"PONG"
            if args[:3] == ["docker", "exec", "-w"] and "__full_version_str__" in args[-1]:
                return b"3.2.1"
            if args[:3] == ["docker", "exec", "-i"]:
                self.assertEqual(json.loads(kwargs["stdin"])["documents.document"]["1"]["original_sha256"], backup.hash_file(self.root / source["id"] / "export" / "original.txt")["sha256"])
                return b'{"ok":true,"documents":1}'
            if args[:2] == ["docker", "inspect"] or args[:3] in (["docker", "network", "inspect"], ["docker", "volume", "inspect"]):
                return label.encode()
            return b""
        result = restore.drill(str(self.config), str(self.root / source["id"] / "receipt.json"), runner=runner)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["cleanup"], "complete")
        schema = json.loads(Path("extensions/library/maintenance/contracts/backup-receipt.schema.json").read_text())
        required = schema["$defs"]["drill"]["allOf"][1]["properties"]["counts"]["required"]
        self.assertEqual(set(result["counts"]), set(required))
        run_commands = [c for c in commands if c[:3] == ["docker", "run", "-d"]]
        self.assertEqual(len(run_commands), 2)
        native = next(c for c in run_commands if NATIVE_IMAGE in c)
        self.assertNotIn("-p", native)
        self.assertNotIn("--publish", native)
        self.assertIn("readonly", " ".join(native))
        self.assertNotIn(str(self.runtime), " ".join(native))
        self.assertEqual(json.loads((self.root / result["id"] / "receipt.json").read_text())["status"], "success")
        self.assertFalse((self.root / result["id"] / "jobs.sqlite").exists())
        if importlib.util.find_spec("jsonschema"):
            import jsonschema
            schema = json.loads(Path("extensions/library/maintenance/contracts/backup-receipt.schema.json").read_text())
            jsonschema.validate(result, schema)

    def test_mocked_import_failure_retains_resources(self):
        source = self.snapshot()
        commands = []
        def runner(args, **kwargs):
            commands.append(args)
            if args[:3] == ["docker", "image", "inspect"]:
                return ("sha256:" + "b" * 64).encode()
            if args[:3] == ["docker", "inspect", "--format"] and args[3] == "{{.Id}}":
                return ("a" * 64).encode()
            if args[:3] == ["docker", "inspect", "--format"] and args[3] == "{{.Image}}":
                return ("sha256:" + "b" * 64).encode()
            if args[:3] == ["docker", "network", "create"]:
                return ("c" * 64).encode()
            if args[:3] == ["docker", "volume", "create"]:
                return args[-1].encode()
            if args[:3] == ["docker", "run", "-d"]:
                return (("d" if "redis-server" in args else "e") * 64).encode()
            if "redis-cli" in args:
                return b"PONG"
            if "__full_version_str__" in args[-1]:
                return b"3.2.1"
            if "document_importer" in args:
                raise backup.MaintenanceError("native operation failed")
            return b""
        with self.assertRaises(restore.RetainedDrillError) as caught:
            restore.drill(str(self.config), str(self.root / source["id"] / "receipt.json"), runner=runner)
        drill_dir = next(p for p in self.root.iterdir() if p.name.startswith("drill-"))
        result = json.loads((drill_dir / "receipt.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cleanup"], "retained")
        self.assertEqual(len(json.loads((drill_dir / "resources.json").read_text())), 5)
        self.assertFalse(any(c[:2] == ["docker", "rm"] for c in commands))
        stderr = io.StringIO()
        with patch.object(restore, "drill", side_effect=caught.exception), contextlib.redirect_stderr(stderr):
            self.assertEqual(restore.main(["--config", str(self.config), "--receipt", str(self.root / source["id"] / "receipt.json")]), 1)
        output = stderr.getvalue()
        self.assertEqual(json.loads(output)["id"], drill_dir.name)
        self.assertEqual(len(json.loads(output)["resources"]), 5)
        self.assertNotIn("synthetic-private-token", output)
        self.assertNotIn(str(self.base), output)
        self.assertNotIn("Private synthetic title", output)
        if importlib.util.find_spec("jsonschema"):
            import jsonschema
            schema = json.loads(Path("extensions/library/maintenance/contracts/backup-receipt.schema.json").read_text())
            jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(result)


if __name__ == "__main__":
    unittest.main()
