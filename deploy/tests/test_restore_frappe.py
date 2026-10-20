"""本机只测隔离、停止和脱敏；真实恢复由 frappe-contract 调同一入口验证。"""

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import restore_frappe as native


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name).resolve()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        Path("sites").mkdir()
        self.source = self.root / "backup"
        self.source.mkdir(mode=0o700)
        for name in native.SITE_FILES:
            (self.source / name).write_bytes(b"synthetic payload")
            (self.source / name).chmod(0o600)
        original = dict(db_name="source", db_password="old-password", encryption_key="old-key")
        (self.source / "site_config.json").write_text(json.dumps(original))
        self.config = Path("sites/recovery.test/site_config.json")
        self.calls = []

    def bench(self, args):
        self.calls.append(args)
        if args[1] == "new-site":
            self.config.parent.mkdir()
            self.created = dict(
                db_name=args[args.index("--db-name") + 1],
                db_password=args[args.index("--db-password") + 1],
                db_host="isolated-db",
            )
            self.config.write_text(json.dumps(self.created))
        else:
            self.assertTrue(json.loads(self.config.read_text())["mute_emails"])

    def restore(self, run=None):
        native.restore(
            "recovery.test", self.source, "root-secret", "admin-secret", run or self.bench
        )

    def test_new_database_keeps_identity_keys_permissions_and_paused_delivery(self):
        self.restore()
        result = json.loads(self.config.read_text())
        self.assertEqual({k: result[k] for k in self.created}, self.created)
        self.assertNotEqual(result["db_name"], "source")
        self.assertNotEqual(result["db_password"], "old-password")
        self.assertEqual(result["encryption_key"], "old-key")
        self.assertEqual(result["eb_wecom_cards"], {"enabled": False})
        self.assertTrue(result["pause_scheduler"])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("--force", self.calls[0])
        self.assertIn("--force", self.calls[1])
        self.assertNotIn("--encryption-key", self.calls[1])
        self.assertEqual(self.calls[2], ["bench", "--site", "recovery.test", "migrate"])
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(len(self.calls), 3)

    def test_preflight_rejects_unsafe_site_path_link_and_public_secret(self):
        for site in ("../live", "--site", "all;exit", "all", ""):
            with self.assertRaises(ValueError):
                native.restore(site, self.source, "root", "admin", self.bench)
        path = self.source / "files.tar"
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.restore()
        path.unlink()
        path.symlink_to(self.source / "private-files.tar")
        with self.assertRaises(ValueError):
            self.restore()
        self.assertFalse(self.calls)

    def test_untrusted_native_passphrase_rejected_before_creating_database(self):
        config = self.source / "site_config.json"
        for key in ("unsafe;command", "unsafe value", "unsafe\nvalue", {"invalid": True}):
            config.write_text(json.dumps(dict(backup_encryption_key=key)))
            with self.assertRaises(ValueError):
                self.restore()
        self.assertFalse(self.calls)

    def test_restore_failure_stops_before_migrate_and_reports_no_command_or_secret(self):
        def failed(args):
            self.bench(args)
            if "restore" in args:
                raise subprocess.CalledProcessError(1, args, stderr=b"private employee")

        output = io.StringIO()
        with self.assertRaises(native.RestoreError) as error:
            self.restore(failed)
        self.assertEqual(str(error.exception), "frappe:restore:CalledProcessError")
        self.assertEqual(len(self.calls), 2)
        with patch.object(native, "restore", side_effect=error.exception), redirect_stdout(output):
            with (
                patch.object(native.sys, "argv", ["restore_frappe.py", "site", "path"]),
                patch.dict(
                    os.environ,
                    EB_RESTORE_DB_ROOT_PASSWORD="root",
                    EB_RESTORE_ADMIN_PASSWORD="admin",
                ),
            ):
                self.assertEqual(native.main(), 1)
        self.assertEqual(output.getvalue(), "frappe:restore:CalledProcessError\n")


if __name__ == "__main__":
    unittest.main()
