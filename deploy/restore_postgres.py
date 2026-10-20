"""隔离项目 PostgreSQL 原生恢复；不导入旧账号、密码或覆盖非空数据库。"""

import json
import re
import shlex
import subprocess
from pathlib import Path

from backup import private
from backup_collect import KEEP, LABEL, CollectionError
from restore_guard import names

PSQL = 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -h /var/run/postgresql -At'


def command(args, source=None):
    if source is not None:
        with Path(source).open("rb") as stream:
            return subprocess.run(
                args, stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True
            ).stdout
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
    ).stdout


def target(project, manifest, container, run):
    """复核项目标签、挂载卷归属、精确镜像和所有应用已停写。"""
    source = manifest["project"]
    if not all(
        isinstance(p, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", p)
        for p in (project, source)
    ):
        raise ValueError("项目名无效")
    if {project + s for s in KEEP} & {source + s for s in KEEP}:
        raise ValueError("不能写入来源项目")
    if not re.fullmatch(r"[a-f0-9]{64}", container):
        raise ValueError("需要完整目标容器 ID")
    for suffix, keep in KEEP.items():
        ids = (
            run(
                [
                    "docker",
                    "ps",
                    "-q",
                    "--filter",
                    "label=" + LABEL + "project=" + project + suffix,
                ]
            )
            .decode()
            .split()
        )
        if ids:
            for item in json.loads(run(["docker", "inspect", *ids])):
                labels = item["Config"]["Labels"]
                if (
                    labels.get(LABEL + "project") != project + suffix
                    or labels.get(LABEL + "service") not in keep
                    or labels.get(LABEL + "oneoff", "false").lower() == "true"
                ):
                    raise ValueError("恢复目标仍有应用或临时任务运行")
    item = json.loads(run(["docker", "inspect", container]))[0]
    labels, state = item["Config"]["Labels"], item["State"]
    if (
        item["Id"] != container
        or labels.get(LABEL + "project") != project + "-dify"
        or labels.get(LABEL + "service") != "db_postgres"
        or labels.get(LABEL + "oneoff", "false").lower() == "true"
        or not state["Running"]
        or state.get("Paused")
        or state.get("Restarting")
        or state.get("Health", {}).get("Status") != "healthy"
        or item["Image"] != manifest["images"]["-dify/db_postgres"]["image_id"]
    ):
        raise ValueError("目标数据库身份、版本或状态不符")
    mounts = [m for m in item["Mounts"] if m["Destination"] == "/var/lib/postgresql/data"]
    if len(mounts) != 1 or mounts[0]["Type"] != "volume" or not mounts[0]["RW"]:
        raise ValueError("目标数据必须使用独立命名卷")
    volume = json.loads(run(["docker", "volume", "inspect", mounts[0]["Name"]]))[0]
    if (volume.get("Labels") or {}).get(LABEL + "project") != project + "-dify":
        raise ValueError("目标数据库卷不属于新项目")


def restore(project, manifest, container, payload, run=command):
    """由完整入口在 restore_guard.prepared 的锁内调用；失败不启动目标应用。"""
    step = "preflight"
    try:
        payload = Path(payload)
        private(payload, directory=True)
        private(payload / "databases.json")
        databases = names(
            json.loads((payload / "databases.json").read_text()), r"[A-Za-z0-9_][A-Za-z0-9_-]*"
        )
        if {"template0", "template1"} & set(databases):
            raise ValueError("不能恢复模板数据库")
        for index in range(len(databases)):
            path = payload / f"postgres-{index}.dump"
            private(path)
            if not path.stat().st_size:
                raise ValueError("数据库备份为空")
        target(project, manifest, container, run)
        prefix = ["docker", "exec", container, "sh", "-ec"]

        def query(database, sql):
            return run(
                [
                    *prefix,
                    PSQL + ' -d "$1" -c ' + shlex.quote(sql),
                    "sh",
                    database,
                ]
            )

        existing = json.loads(query("postgres", "SELECT json_agg(datname) FROM pg_database"))
        # 全部预检后才创建/写入库，拒绝已启动应用初始化过的数据。
        empty = (
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')"
        )
        for database in databases:
            if database in existing and int(query(database, empty)) != 0:
                raise ValueError("目标数据库不是空库")
        step = "restore"
        for index, database in enumerate(databases):
            if database not in existing:
                run(
                    [
                        *prefix,
                        'exec createdb -U "$POSTGRES_USER" -h /var/run/postgresql '
                        '--maintenance-db=postgres --owner "$POSTGRES_USER" -- "$1"',
                        "sh",
                        database,
                    ]
                )
            # 不执行 pg_dumpall globals：账号及口令继续使用目标容器配置。
            # 所有对象归目标角色，不重建源 ACL、源 owner 或源表空间路径。
            run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "sh",
                    "-ec",
                    'exec pg_restore -U "$POSTGRES_USER" -h /var/run/postgresql '
                    "--no-owner --no-privileges --no-tablespaces --single-transaction "
                    '--exit-on-error --dbname "$1"',
                    "sh",
                    database,
                ],
                payload / f"postgres-{index}.dump",
            )
    except Exception as error:
        raise CollectionError(f"PostgreSQL:{step}:{type(error).__name__}") from None
