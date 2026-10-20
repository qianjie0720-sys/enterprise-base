"""P3-G2 原生数据收集；调用方负责独占锁、私有暂存目录、加密和清理。"""

import json
import os
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
KEEP = {
    "": set(),
    "-hr": {"db", "redis-cache", "redis-queue"},
    "-dify": {"db_postgres", "redis", "qdrant"},
}
LABEL = "com.docker.compose."
QDRANT = """import hashlib, json, os, shutil, sys, urllib.request, urllib.parse
base = os.environ["QDRANT_URL"].rstrip("/")
assert base == "http://qdrant:6333"
headers = {"api-key": os.environ["QDRANT_API_KEY"]}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def request(path, method="GET"):
    req = urllib.request.Request(base + path, headers=headers, method=method)
    return opener.open(req, timeout=600)
with request("/cluster") as response:
    assert json.load(response)["result"]["status"] == "disabled"
with request("/snapshots?wait=true", "POST") as response:
    info = json.load(response)["result"]
path = "/snapshots/" + urllib.parse.quote(info["name"], safe="")
try:
    digest, size = hashlib.sha256(), 0
    with request(path) as response:
        while chunk := response.read(1024 * 1024):
            sys.stdout.buffer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    assert size == info["size"]
    assert not info.get("checksum") or digest.hexdigest() == info["checksum"]
finally:
    with request(path + "?wait=true", "DELETE") as response:
        assert json.load(response)["result"] is True
"""
SITES = (
    "import json,pathlib; print(json.dumps(sorted(p.parent.name for p in "
    'pathlib.Path("sites").glob("*/site_config.json"))))'
)


class CollectionError(RuntimeError):
    """只包含组件及错误类型，不带容器输出、命令或业务内容。"""


def command(args, output=None):
    return subprocess.run(
        args,
        stdout=output or subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        cwd=ROOT,
        check=True,
    ).stdout


class Collector:
    def __init__(self, project, run=command, sleep=time.sleep):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
            raise CollectionError("配置:ValueError")
        self.project, self.run, self.sleep = project, run, sleep
        self.containers, self.images, self.networks = {}, {}, {}

    def call(self, component, args, output=None):
        try:
            return self.run(["docker", *args], output)
        except Exception as error:
            raise CollectionError(f"{component}:{type(error).__name__}") from None

    def reader(self, suffix, service, args, output=None, entry="python"):
        item = self.containers[suffix, service]
        options = (
            ["--volumes-from", item["Id"]]
            if suffix == "-hr"
            else ["--env-file", str(ROOT / "secrets/dify.env")]
        )
        options = [
            "create",
            "--log-driver",
            "none",
            "--network",
            self.networks[suffix],
            *options,
            "--entrypoint",
            entry,
            item["Image"],
            *args,
        ]
        created = self.call(service, options).decode().strip()
        try:
            return self.call(service, ["start", "-a", created], output)
        finally:
            self.call(service, ["rm", "-f", created])

    def discover(self):
        for suffix in KEEP:
            ids = self.call(
                "Docker",
                ["ps", "-aq", "--filter", "label=" + LABEL + "project=" + self.project + suffix],
            ).split()
            if not ids:
                raise CollectionError("Docker:MissingProject")
            records = json.loads(self.call("Docker", ["inspect", *(i.decode() for i in ids)]))
            for item in records:
                labels, state = item["Config"]["Labels"], item["State"]
                if labels[LABEL + "project"] != self.project + suffix:
                    raise CollectionError("Docker:ProjectMismatch")
                if labels.get(LABEL + "oneoff", "false").lower() == "true":
                    if state["Running"]:
                        raise CollectionError("Docker:ConcurrentJob")
                    continue
                key = (suffix, labels[LABEL + "service"])
                if key in self.containers or state.get("Paused") or state.get("Restarting"):
                    raise CollectionError("Docker:UnsupportedState")
                self.containers[key] = item
                self.images[suffix + "/" + key[1]] = dict(
                    image=item["Config"]["Image"], image_id=item["Image"]
                )
        for key in (
            ("", "caddy"),
            ("-hr", "backend"),
            ("-hr", "db"),
            ("-dify", "api"),
            ("-dify", "plugin_daemon"),
            ("-dify", "db_postgres"),
            ("-dify", "qdrant"),
        ):
            if key not in self.containers or not self.containers[key]["State"]["Running"]:
                raise CollectionError("Docker:MissingRunningService")
        for suffix, service in (("-hr", "backend"), ("-dify", "api")):
            networks = self.containers[suffix, service]["NetworkSettings"]["Networks"]
            matches = []
            for name in networks:
                record = json.loads(self.call("Docker", ["network", "inspect", name]))[0]
                labels = record.get("Labels") or {}
                if (
                    labels.get(LABEL + "project") == self.project + suffix
                    and labels.get(LABEL + "network") == "default"
                ):
                    matches.append(record["Id"])
            if len(matches) != 1:
                raise CollectionError("Docker:InvalidNetwork")
            self.networks[suffix] = matches[0]
        return self.images

    @contextmanager
    def stopped(self):
        # 先记下全部 ID：stop 部分成功后也必须尝试恢复。
        attempted = [
            item["Id"]
            for (suffix, service), item in self.containers.items()
            if service not in KEEP[suffix] and item["State"]["Running"]
        ]
        try:
            if attempted:
                # 部分上游镜像入口不转发 SIGTERM，要等满超时；一次传入让 Docker 并行停止。
                self.call("停止服务", ["stop", "--time", "60", *attempted])
            yield
        finally:
            failed = False
            for identity in reversed(attempted):
                try:
                    self.call("恢复服务", ["start", identity])
                except CollectionError:
                    failed = True  # 某个启动失败不能阻断其它服务恢复。
            if failed:
                raise CollectionError("恢复服务:StartFailed")
            if attempted:
                self.healthy(attempted)

    def healthy(self, identities):
        """docker start 不等依赖和健康检查；全部健康后才算恢复完成。"""
        for _ in range(60):
            records = json.loads(self.call("恢复服务", ["inspect", *identities]))
            states = [record["State"] for record in records]
            if any(not state["Running"] for state in states):
                raise CollectionError("恢复服务:Exited")
            if all(
                state.get("Health", {}).get("Status", "healthy") == "healthy" for state in states
            ):
                return
            self.sleep(5)
        raise CollectionError("恢复服务:Unhealthy")

    def payload(self, folder, name, component, args):
        with (folder / name).open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            self.call(component, args, output)

    def collect(self, destination):
        folder = Path(destination)
        if (
            folder.is_symlink()
            or not folder.is_dir()
            or folder.stat().st_mode & 0o077
            or any(folder.iterdir())
        ):
            raise CollectionError("暂存:UnsafeDirectory")
        self.discover()
        with self.stopped():
            self.frappe(folder)
            self.postgres(folder)
            for service, path in (("api", "/app/api/storage"), ("plugin_daemon", "/app/storage")):
                self.payload(
                    folder,
                    service + ".tar",
                    service,
                    ["cp", self.containers["-dify", service]["Id"] + ":" + path + "/.", "-"],
                )
            # 证书和企业微信会话不在数据库里；停写后一起复制，wechat 未启用时没有容器。
            for suffix, service, path in (
                ("", "caddy", "/data"),
                ("", "wechat", "/app/data"),
            ):
                if (suffix, service) in self.containers:
                    self.payload(
                        folder,
                        service + ".tar",
                        service,
                        [
                            "cp",
                            self.containers[suffix, service]["Id"] + ":" + path + "/.",
                            "-",
                        ],
                    )
            with (folder / "qdrant.snapshot").open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                self.reader("-dify", "api", ["-c", QDRANT], output)
        return self.images

    def frappe(self, folder):
        sites = json.loads(self.reader("-hr", "backend", ["-c", SITES]))
        if not sites or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", s) for s in sites):
            raise CollectionError("Frappe:InvalidSites")
        work = "/home/frappe/frappe-bench/sites/.eb-backup-" + uuid4().hex
        succeeded = False
        try:
            for index, site in enumerate(sites):
                base = work + "/" + str(index)
                args = [
                    "--site",
                    site,
                    "backup",
                    "--with-files",
                    "--ignore-backup-conf",
                    "--backup-path",
                    base,
                ]
                for flag, filename in (
                    ("db", "database.sql.gz"),
                    ("files", "files.tar"),
                    ("private-files", "private-files.tar"),
                    ("conf", "site_config.json"),
                ):
                    args += ["--backup-path-" + flag, base + "/" + filename]
                program = "import os,sys;os.umask(0o077);os.execvp('bench',['bench',*sys.argv[1:]])"
                self.reader("-hr", "backend", ["-c", program, *args])
                # 首次原生加密备份可能在备份配置后才生成密钥，重新取最新配置。
                self.reader(
                    "-hr",
                    "backend",
                    [
                        "-c",
                        "import shutil,sys;shutil.copyfile(sys.argv[1],sys.argv[2])",
                        "sites/" + site + "/site_config.json",
                        base + "/site_config.json",
                    ],
                )
            (folder / "sites.json").write_text(json.dumps(sites))
            (folder / "sites.json").chmod(0o600)
            self.payload(
                folder,
                "frappe.tar",
                "Frappe",
                ["cp", self.containers["-hr", "backend"]["Id"] + ":" + work + "/.", "-"],
            )
            succeeded = True
        finally:
            # 连同以前失败留下的明文副本一起删；备份已失败时保留原错误，不让清理错误盖掉它。
            cleanup = (
                "import glob,shutil;[shutil.rmtree(p) for p in "
                "glob.glob('/home/frappe/frappe-bench/sites/.eb-backup-*')]"
            )
            try:
                self.reader("-hr", "backend", ["-c", cleanup])
            except CollectionError:
                if succeeded:
                    raise

    def postgres(self, folder):
        container = self.containers["-dify", "db_postgres"]["Id"]
        prefix = ["exec", container, "sh", "-ec"]
        sql = (
            "SELECT coalesce(json_agg(datname ORDER BY datname), '[]'::json) "
            "FROM pg_database WHERE NOT datistemplate"
        )
        raw = self.call(
            "PostgreSQL",
            [*prefix, 'exec psql -U "$POSTGRES_USER" -d postgres -Atc ' + shlex.quote(sql)],
        )
        databases = json.loads(raw)
        if not databases or not all(
            isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", name)
            for name in databases
        ):
            raise CollectionError("PostgreSQL:InvalidDatabases")
        (folder / "databases.json").write_text(json.dumps(databases))
        (folder / "databases.json").chmod(0o600)
        for index, name in enumerate(databases):
            self.payload(
                folder,
                f"postgres-{index}.dump",
                "PostgreSQL",
                [*prefix, 'exec pg_dump -U "$POSTGRES_USER" -Fc --dbname "$1"', "sh", name],
            )
        self.payload(
            folder,
            "postgres-globals.sql",
            "PostgreSQL",
            [*prefix, 'exec pg_dumpall -U "$POSTGRES_USER" --globals-only'],
        )
