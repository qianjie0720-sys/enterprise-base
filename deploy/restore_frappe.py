"""在隔离 Frappe 容器的 bench 根目录恢复新站点；调用方负责停写和清理暂存区。"""

import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from backup import private
from restore_guard import SITE_FILES, site_settings


class RestoreError(RuntimeError):
    """只报告步骤与异常类型，不包含原生命令、凭据或业务输出。"""


def command(args):
    subprocess.run(
        args,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def settings(path, value):
    """原子替换，配置写盘时始终为 600。"""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def restore(site, source, root_password, admin_password, run=command):
    if site == "all" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", site):
        raise ValueError("目标站点名无效")
    if not root_password or not admin_password:
        raise ValueError("恢复口令不能为空")
    source = Path(source).absolute()
    # 原生 bench 使用 shell 检测备份类型；不允许路径含 shell 字符或链接。
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(source)) or any(
        p.is_symlink() for p in (source, *source.parents)
    ):
        raise ValueError("备份目录路径不安全")
    source = source.resolve(strict=True)
    private(source, directory=True)
    for name in SITE_FILES:
        path = source / name
        private(path)
        if not path.stat().st_size:
            raise ValueError("备份文件为空")
    original = json.loads((source / "site_config.json").read_text())
    if not isinstance(original, dict):
        raise ValueError("源配置不是对象")
    # 在任何数据库写入前校验密钥类型。
    site_settings(original, dict(db_name="preflight", db_password="preflight"))
    backup_key = original.get("backup_encryption_key")
    if backup_key and not re.fullmatch(r"[A-Za-z0-9_=-]{1,256}", backup_key):
        raise ValueError("原生备份密钥包含不安全字符")
    target = Path("sites") / site
    if Path("sites").is_symlink() or target.exists() or target.is_symlink():
        raise ValueError("只能恢复到不存在的新站点")
    with os.fdopen(
        os.open("sites/.eb-restore.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "rb"
    ) as lock:
        private(Path("sites/.eb-restore.lock"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 获取锁后再检查，拒绝并发调用复用已经建立的站点。
        if target.exists() or target.is_symlink():
            raise ValueError("目标站点已存在")
        database = "_ebrestore_" + uuid4().hex[:16]
        password = secrets.token_urlsafe(32)
        step = "new-site"
        try:
            run(
                [
                    "bench",
                    "new-site",
                    site,
                    "--db-name",
                    database,
                    "--db-password",
                    password,
                    "--db-root-password",
                    root_password,
                    "--admin-password",
                    admin_password,
                    "--mariadb-user-host-login-scope",
                    "%",
                ]
            )
            config = target / "site_config.json"
            current = json.loads(config.read_text())
            if current.get("db_name") != database or current.get("db_password") != password:
                raise ValueError("新站点数据库身份不符")
            if database == original.get("db_name") or password == original.get("db_password"):
                raise ValueError("新旧数据库身份必须隔离")
            settings(config, site_settings(original, current))
            step = "restore"
            # --force 仅作用于上面刚创建的空库；不传 --encryption-key，
            # 由原生 SQL 类型检测决定是否使用配置中的 backup_encryption_key 解密附件。
            run(
                [
                    "bench",
                    "--site",
                    site,
                    "restore",
                    str(source / "database.sql.gz"),
                    "--force",
                    "--db-root-password",
                    root_password,
                    "--admin-password",
                    admin_password,
                    "--with-public-files",
                    str(source / "files.tar"),
                    "--with-private-files",
                    str(source / "private-files.tar"),
                ]
            )
            current = json.loads(config.read_text())
            if current.get("db_name") != database or current.get("db_password") != password:
                raise ValueError("恢复改写了目标数据库身份")
            settings(config, site_settings(original, current))
            step = "migrate"
            run(["bench", "--site", site, "migrate"])
        except Exception as error:
            raise RestoreError(f"frappe:{step}:{type(error).__name__}") from None


def main():
    os.umask(0o077)
    try:
        if len(sys.argv) != 3:
            raise ValueError("需要目标站点和容器内备份目录")
        restore(
            sys.argv[1],
            sys.argv[2],
            os.environ["EB_RESTORE_DB_ROOT_PASSWORD"],
            os.environ["EB_RESTORE_ADMIN_PASSWORD"],
        )
    except Exception as error:
        print(str(error) if isinstance(error, RestoreError) else f"frappe:{type(error).__name__}")
        return 1
    print("Frappe 新站点原生恢复及迁移完成；调度、邮件和审批卡片保持关闭。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
