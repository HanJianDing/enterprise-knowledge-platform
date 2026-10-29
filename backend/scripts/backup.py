"""自动备份脚本（产品化 C1）：PostgreSQL 全量 dump + Chroma 目录 + uploads 目录

用法（backend 目录下）：
    python scripts/backup.py                        # 备份到 ../backups/，保留最近 14 天
    python scripts/backup.py --keep 7 --backup-dir D:/backups

部署（任选其一，每天一次）：
    Linux cron：0 3 * * * cd /srv/app/backend && python scripts/backup.py >> backup.log 2>&1
    Windows 任务计划：每天执行同一命令
    恢复演练见 docs/运维手册-备份与监控.md——没演练过的备份等于没有备份。

pg_dump 优先走 docker exec（BACKUP_PG_CONTAINER 指定数据库容器名，docker 部署推荐），
否则调本机 pg_dump（需在 PATH 或 BACKUP_PG_BIN 指定路径）。
"""
import argparse
import os
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_DIR = BACKEND_DIR.parent / "backups"


def parse_db_url(url: str) -> dict:
    """解析 SQLAlchemy URL（postgresql+asyncpg://user:pass@host:port/db）→ pg_dump 参数。

    用户/密码必须 percent-decode：URL 中的特殊字符（@ 为 %40 等）不解码的话
    pg_dump 拿到的是编码串、认证静默失败（SQLAlchemy 自己会解码，应用不受影响）。
    """
    parsed = urllib.parse.urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
        "db": urllib.parse.unquote(parsed.path.lstrip("/") or ""),
    }


def cleanup_old_backups(backup_dir: Path, keep: int) -> int:
    """按目录名时间戳排序，删除超出保留数量的最旧备份，返回删除数。

    纯函数（不依赖外部命令），单测覆盖保留/清理边界。
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return 0
    stamps = sorted(
        (p for p in backup_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )
    removed = 0
    for old in stamps[:-keep] if keep > 0 else stamps:
        shutil.rmtree(old, ignore_errors=True)
        removed += 1
    return removed


def backup_pg(out_sql: Path, db_url: str, container: str | None, pg_bin: str | None) -> bool:
    """PostgreSQL 全量 dump；失败返回 False（不抛异常，由调用方汇总报告）"""
    info = parse_db_url(db_url)
    if container:
        cmd = [
            "docker", "exec", container, "pg_dump",
            "-U", info["user"], "-d", info["db"], "--no-owner",
        ]
    else:
        pg_dump = (Path(pg_bin) / "pg_dump") if pg_bin else "pg_dump"
        cmd = [
            str(pg_dump), "-h", info["host"], "-p", info["port"],
            "-U", info["user"], "-d", info["db"], "--no-owner",
        ]
    env = dict(os.environ)
    if not container:
        env["PGPASSWORD"] = info["password"]
    try:
        with open(out_sql, "wb") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, check=True, env=env)
        try:
            os.chmod(out_sql, 0o600)  # 备份含业务数据，收紧权限（Windows 上为 no-op）
        except OSError:
            pass
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  [pg] 备份失败: {e}", file=sys.stderr)
        return False


def backup_dir_tree(src: Path, out_archive: str) -> bool:
    """目录打包为 zip/tar（shutil.make_archive）；目录不存在返回 False"""
    if not Path(src).exists():
        print(f"  [dir] 目录不存在，跳过: {src}", file=sys.stderr)
        return False
    shutil.make_archive(out_archive, "zip", root_dir=src)
    return True


def run_backup(
    backup_dir: Path,
    keep: int,
    db_url: str,
    container: str | None = None,
    pg_bin: str | None = None,
    chroma_dir: Path | None = None,
    upload_dir: Path | None = None,
) -> dict:
    """执行一次完整备份，返回结果摘要（各组件成功与否）"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = Path(backup_dir) / stamp
    target.mkdir(parents=True, exist_ok=True)

    summary = {"stamp": stamp, "dir": str(target), "pg": False, "chroma": False, "uploads": False}

    summary["pg"] = backup_pg(target / "db.sql", db_url, container, pg_bin)
    chroma_dir = Path(chroma_dir) if chroma_dir else BACKEND_DIR / "chroma_db"
    upload_dir = Path(upload_dir) if upload_dir else BACKEND_DIR / "uploads"
    if summary["pg"]:
        summary["chroma"] = backup_dir_tree(chroma_dir, str(target / "chroma"))
        summary["uploads"] = backup_dir_tree(upload_dir, str(target / "uploads"))
    else:
        # 库都备份不了，删掉本次目录避免留下半成品
        shutil.rmtree(target, ignore_errors=True)
        return {**summary, "dir": "(已清理)"}

    removed = cleanup_old_backups(backup_dir, keep)
    summary["removed_old"] = removed
    return summary


def resolve_db_url(env_file: Path | None = None) -> str:
    """数据库连接串：环境变量优先，其次 .env 文件回退（验收修复 A7）。

    此前该逻辑放在判空退出之后，未设环境变量时 .env 回退永远执行不到。
    env_file 参数供测试注入临时文件；默认用项目根目录 .env。
    """
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url
    env_file = env_file or (BACKEND_DIR.parent / ".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1]
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="企业知识库自动备份")
    parser.add_argument("--keep", type=int, default=14, help="保留最近 N 份备份（默认 14，最小 1）")
    parser.add_argument("--backup-dir", type=str, default=str(DEFAULT_BACKUP_DIR))
    args = parser.parse_args()
    if args.keep < 1:
        parser.error("--keep 最小为 1（0 会把刚生成的备份也清掉）")

    db_url = resolve_db_url()
    if not db_url:
        print("未设置 DATABASE_URL，无法备份数据库", file=sys.stderr)
        sys.exit(1)

    # 目录支持环境变量覆盖（演示环境指到 chroma_demo/uploads_demo，防误备份真实目录）
    chroma_dir = os.environ.get("CHROMA_PERSIST_DIR")
    upload_dir = os.environ.get("UPLOAD_DIR")

    summary = run_backup(
        Path(args.backup_dir), args.keep, db_url,
        container=os.environ.get("BACKUP_PG_CONTAINER"),
        pg_bin=os.environ.get("BACKUP_PG_BIN"),
        chroma_dir=Path(chroma_dir) if chroma_dir else None,
        upload_dir=Path(upload_dir) if upload_dir else None,
    )
    print(
        f"备份完成: {summary['dir']}\n"
        f"  数据库: {'OK' if summary['pg'] else 'FAILED'}"
        f" | 向量库: {'OK' if summary['chroma'] else '跳过'}"
        f" | 附件: {'OK' if summary['uploads'] else '跳过'}"
        f" | 清理旧备份: {summary.get('removed_old', 0)} 份"
    )
    sys.exit(0 if summary["pg"] else 1)


if __name__ == "__main__":
    main()
