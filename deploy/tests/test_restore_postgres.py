"""PostgreSQL 恢复命令协议与失败停止；真实整套恢复由 Claude 云端补测。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import restore_postgres as pg


class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.payload = Path(self.work.name)
        (self.payload / "databases.json").write_text('["dify","dify_plugin","postgres"]')
        for index in range(3):
            (self.payload / f"postgres-{index}.dump").write_bytes(b"synthetic dump")
        for path in self.payload.iterdir():
            path.chmod(0o600)
        self.identity = "a" * 64
        self.item: dict[str, Any] = dict(
            Id=self.identity,
            Image="sha256:pinned",
            State=dict(Running=True, Health=dict(Status="healthy")),
            Config=dict(
                Labels={pg.LABEL + "project": "recovery-dify", pg.LABEL + "service": "db_postgres"}
            ),
            Mounts=[
                dict(
                    Destination="/var/lib/postgresql/data",
                    Type="volume",
                    RW=True,
                    Name="recovery_pg",
                )
            ],
        )
        self.manifest = dict(
            project="source", images={"-dify/db_postgres": dict(image_id="sha256:pinned")}
        )
        self.volume = dict(Labels={pg.LABEL + "project": "recovery-dify"})
        self.calls, self.dirty, self.failed = [], False, False

    def docker(self, args, source=None):
        self.calls.append((args, source))
        if args[1] == "ps":
            return self.identity.encode() if args[-1].endswith("recovery-dify") else b""
        if args[1] == "inspect":
            return json.dumps([self.item]).encode()
        if args[1] == "volume":
            return json.dumps([self.volume]).encode()
        if any("json_agg" in a for a in args):
            return b'["postgres", "dify"]'
        if any("count(*)" in a for a in args):
            return b"1" if self.dirty else b"0"
        if source is not None and self.failed:
            raise subprocess.CalledProcessError(1, args, stderr=b"private secret employee")
        return b""

    def restore(self):
        pg.restore("recovery", self.manifest, self.identity, self.payload, self.docker)

    def test_all_databases_stream_into_new_owner_without_importing_old_roles(self):
        self.restore()
        writes = [(args, path) for args, path in self.calls if path is not None]
        self.assertEqual([args[-1] for args, _ in writes], ["dify", "dify_plugin", "postgres"])
        self.assertEqual(
            [path.name for _, path in writes], [f"postgres-{i}.dump" for i in range(3)]
        )
        for args, _ in writes:
            script = args[-3]
            for flag in (
                "--no-owner",
                "--no-privileges",
                "--no-tablespaces",
                "--single-transaction",
                "--exit-on-error",
            ):
                self.assertIn(flag, script)
            self.assertNotIn("--clean", script)
        self.assertEqual(sum(any("createdb" in a for a in c) for c, _ in self.calls), 1)
        self.assertFalse(
            any("postgres-globals.sql" in a or "--password" in a for c, _ in self.calls for a in c)
        )

    def test_source_project_running_application_shared_volume_image_and_dirty_db_rejected(self):
        for defect in ("source", "writer", "volume", "image", "dirty"):
            with self.subTest(defect=defect):
                previous = json.loads(json.dumps((self.manifest, self.item, self.volume)))
                if defect == "source":
                    self.manifest["project"] = "recovery-hr"
                if defect == "writer":
                    self.item["Config"]["Labels"][pg.LABEL + "service"] = "api"
                if defect == "volume":
                    self.volume["Labels"][pg.LABEL + "project"] = "source-dify"
                if defect == "image":
                    self.item["Image"] = "sha256:other-version"
                self.dirty = defect == "dirty"
                self.calls.clear()
                with self.assertRaises(pg.CollectionError):
                    self.restore()
                self.assertFalse(
                    any(path or any("createdb" in a for a in c) for c, path in self.calls)
                )
                self.manifest, self.item, self.volume = previous

    def test_bad_index_public_dump_and_native_failure_stop_without_leaking(self):
        index = self.payload / "databases.json"
        index.write_text('["host=outside dbname=live"]')
        with self.assertRaises(pg.CollectionError):
            self.restore()
        self.assertFalse(self.calls)
        index.write_text('["dify","dify_plugin","postgres"]')
        dump = self.payload / "postgres-0.dump"
        dump.chmod(0o644)
        with self.assertRaises(pg.CollectionError):
            self.restore()
        self.assertFalse(self.calls)
        dump.chmod(0o600)
        self.failed = True
        with self.assertRaises(pg.CollectionError) as error:
            self.restore()
        self.assertEqual(str(error.exception), "PostgreSQL:restore:CalledProcessError")
        self.assertEqual(sum(path is not None for _, path in self.calls), 1)

    def test_command_streams_file_to_process_stdin(self):
        result = pg.command(
            [sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            self.payload / "postgres-0.dump",
        )
        self.assertEqual(result, b"synthetic dump")


if __name__ == "__main__":
    unittest.main()
