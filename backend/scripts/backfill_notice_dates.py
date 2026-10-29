"""历史通知 created_at 回填脚本（批次语料增强 P1-1 修复）

8i 时效衰减按文档 created_at 天数计算——历史通知（2023/2024/2026 年内）
上传时间=今天会导致衰减系数全 1.0（"过期通知不降权"演示失效）。
本脚本读取 gen_realistic_docs.py 生成的 D:/文档数据/_backdates.json
（{文件名: 回填日期} 清单），把已入库通知的 created_at 回填为历史日期。

用法（backend 目录下，先跑 gen_realistic_docs.py 生成语料与清单）：
    python scripts/backfill_notice_dates.py          # 演示隔离库
    python scripts/backfill_notice_dates.py --target real --yes  # 真实库
幂等：同文件名重复回填无害（值相同）；清单不存在的文件名跳过。
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT = BACKEND_DIR.parent

# 先解析 --target（在环境隔离之前决定指向哪个库）——seed_demo.py 同款
_TARGET_PARSER = argparse.ArgumentParser(add_help=False)
_TARGET_PARSER.add_argument("--target", choices=["demo", "real"], default="demo")
_TARGET_PARSER.add_argument("--yes", action="store_true")
_target_args, _ = _TARGET_PARSER.parse_known_args()
TARGET_REAL = _target_args.target == "real"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env(ROOT / ".env")  # 真实密钥（DASHSCOPE_API_KEY 与真实库地址）
import urllib.parse

if not TARGET_REAL:
    # 演示模式：隔离变量指向演示库/目录（与 seed_demo.py 同款——
    # 质检终轮补审：脚本宣称 demo 隔离但此前未切环境，默认命令会误改真实库）
    _load_env(ROOT / ".env.demo")
    _real_url = os.environ.get("DATABASE_URL", "")
    _parsed = urllib.parse.urlparse(_real_url)
    os.environ["DATABASE_URL"] = _parsed._replace(path="/ragdb_demo").geturl()
    os.environ["CHROMA_PERSIST_DIR"] = str(BACKEND_DIR / "chroma_demo")
    os.environ["UPLOAD_DIR"] = str(BACKEND_DIR / "uploads_demo")

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

BACKDATES_FILE = Path(r"D:\文档数据") / "_backdates.json"


def _load_backdates() -> dict[str, str]:
    if not BACKDATES_FILE.exists():
        print(f"清单不存在: {BACKDATES_FILE}（先跑 gen_realistic_docs.py 生成）")
        return {}
    return json.loads(BACKDATES_FILE.read_text(encoding="utf-8"))


async def main() -> None:
    from sqlalchemy import select, update

    from app.infrastructure.database import async_session_factory
    from app.models.document import Document

    env_name = "真实库（--target real）" if TARGET_REAL else "演示隔离库"
    _u = urllib.parse.urlparse(os.environ.get("DATABASE_URL", ""))
    _masked = _u._replace(netloc=f"{_u.username or '?'}:***@{_u.hostname}:{_u.port or 5432}").geturl()
    print(f"目标环境：{env_name}（数据库: {_masked}）")

    backdates = _load_backdates()
    if not backdates:
        return
    async with async_session_factory() as db:
        updated = 0
        for fname, date_str in backdates.items():
            row = (await db.execute(select(Document.id, Document.created_at).where(
                Document.original_name == fname
            ))).first()
            if row is None:
                print(f"跳过（库中无此文件）: {fname}")
                continue
            target = datetime.fromisoformat(date_str + "T09:00:00")
            if row[1] is not None and row[1].date() == target.date():
                print(f"已回填（跳过）: {fname}")
                continue
            await db.execute(update(Document).where(Document.id == row[0]).values(
                created_at=target
            ))
            updated += 1
            print(f"✓ 回填 {date_str}: {fname}")
        await db.commit()
        print(f"回填完成：更新 {updated} 份")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="历史通知 created_at 回填")
    parser.add_argument("--target", choices=["demo", "real"], default="demo",
                        help="demo=演示隔离环境（默认）；real=真实库（需 --yes）")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.target == "real" and not args.yes:
        parser.error("--target real 会修改真实库，需加 --yes 确认")
    asyncio.run(main())
