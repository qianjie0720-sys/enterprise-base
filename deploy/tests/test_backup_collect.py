"""原生命令协议和故障恢复；不将替身执行冒充整套服务实测。"""

import json
import tempfile
import unittest
from pathlib import Path

from backup_collect import LABEL, QDRANT, SITES, CollectionError, Collector


class Docker:
    def __init__(self, failure=""):
        self.calls, self.readers = [], {}
        self.failures = failure if isinstance(failure, tuple) else (failure,) * bool(failure)
        self.items = json.loads(Path(__file__).with_name("backup-containers.json").read_text())

    def __call__(self, args, output=None):
        args = args[1:]
        self.calls.append(args)
        effective = self.readers[args[2]] if args[:2] == ["start", "-a"] else args
        for marker in self.failures:
            if any(marker in arg for arg in effective):
                # 清理失败用另一种错误类型，便于确认原错误没有被它盖掉。
                raise (PermissionError if marker == "rmtree" else OSError)("PRIVATE=must-not-leak")
        if args[0] == "ps":
            project = args[-1].split("=")[-1]
            return "\n".join(
                k
                for k, v in self.items.items()
                if v["Config"]["Labels"][LABEL + "project"] == project
            ).encode()
        if args[0] == "inspect":
            return json.dumps([self.items[k] for k in args[1:]]).encode()
        if args[0] == "network":
            return json.dumps(
                [
                    dict(
                        Id=args[-1],
                        Labels={LABEL + "project": args[-1], LABEL + "network": "default"},
                    )
                ]
            ).encode()
        if args[0] == "create":
            identity = "reader-" + str(len(self.readers))
            self.readers[identity] = args
            return identity.encode()
        if SITES in effective:
            return b'["hr.customer.test", "second.customer.test"]'
        if any("json_agg" in arg for arg in args):
            return b'["dify", "dify_plugin", "postgres"]'
        if output:
            output.write(b"native-payload")
        return b""


class CollectionTests(unittest.TestCase):
    def execute(self, docker, folder):
        return Collector("customer", run=docker, sleep=lambda _: None).collect(folder)

    def resumed(self, docker):
        stops = [c for c in docker.calls if c[0] == "stop"]
        self.assertEqual(len(stops), 1)  # 一次传入全部容器，由 Docker 并行停止
        stopped = stops[0][3:]
        started = [c[-1] for c in docker.calls if c[0] == "start" and "-a" not in c]
        self.assertEqual(set(stopped), set(started))
        self.assertNotIn("/wechat", started)
        self.assertFalse(any(k.endswith(("/db", "/db_postgres", "/qdrant")) for k in stopped))
        created = {c[-1] for c in docker.calls if c[0] == "rm"}
        self.assertEqual(set(docker.readers), created)

    def test_all_sites_databases_and_private_payloads(self):
        docker = Docker()
        with tempfile.TemporaryDirectory() as work:
            images = self.execute(docker, work)
            names = {p.name for p in Path(work).iterdir()}
            self.assertEqual(
                names,
                {
                    "frappe.tar",
                    "sites.json",
                    "api.tar",
                    "caddy.tar",
                    "wechat.tar",
                    "plugin_daemon.tar",
                    "databases.json",
                    "postgres-globals.sql",
                    "qdrant.snapshot",
                    "postgres-0.dump",
                    "postgres-1.dump",
                    "postgres-2.dump",
                },
            )
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in Path(work).iterdir()))
            self.assertNotIn("must-not-leak", json.dumps(images))
        native = list(docker.readers.values())
        self.assertEqual(sum("--with-files" in c for c in native), 2)
        copies = [c for c in native if any("shutil.copyfile" in a for a in c)]
        self.assertEqual(len(copies), 2)
        self.assertTrue(
            all(c[-2].startswith("sites/") and c[-1].endswith("site_config.json") for c in copies)
        )
        self.assertTrue(any(QDRANT in c for c in native))
        self.assertTrue(all("sha256:fixed" in c and "none" in c for c in native))
        self.assertEqual(sum(any("pg_dump -U" in a for a in c) for c in docker.calls), 3)
        self.resumed(docker)

    def test_each_failure_resumes_and_redacts(self):
        for marker in ("--with-files", "pg_dump -U", "/app/api/storage", QDRANT, "stop", "start"):
            with self.subTest(marker=marker[:20]), tempfile.TemporaryDirectory() as work:
                docker = Docker(marker)
                with self.assertRaises(CollectionError) as caught:
                    self.execute(docker, work)
                self.assertNotIn("must-not-leak", str(caught.exception))
                self.resumed(docker)

    def test_original_error_survives_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as work:
            docker = Docker(("--with-files", "rmtree"))
            with self.assertRaisesRegex(CollectionError, "^backend:OSError$"):
                self.execute(docker, work)
            self.resumed(docker)
            docker = Docker("rmtree")
            with self.assertRaisesRegex(CollectionError, "^backend:PermissionError$"):
                self.execute(docker, work)
            self.resumed(docker)

    def test_waits_until_restarted_services_are_healthy(self):
        with tempfile.TemporaryDirectory() as work:
            docker = Docker()
            docker.items["-hr/backend"]["State"]["Health"] = {"Status": "starting"}
            with self.assertRaisesRegex(CollectionError, "Unhealthy"):
                self.execute(docker, work)
            self.resumed(docker)

    def test_rejects_running_job_and_unsafe_destination_before_stopping(self):
        with tempfile.TemporaryDirectory() as work:
            docker = Docker()
            docker.items["-hr/backend"]["Config"]["Labels"][LABEL + "oneoff"] = "True"
            with self.assertRaisesRegex(CollectionError, "ConcurrentJob"):
                self.execute(docker, work)
            self.assertFalse(any(c[0] == "stop" for c in docker.calls))
            Path(work, "existing").touch()
            with self.assertRaisesRegex(CollectionError, "UnsafeDirectory"):
                self.execute(Docker(), work)
