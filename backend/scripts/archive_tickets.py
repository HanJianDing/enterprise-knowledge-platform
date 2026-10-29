"""客服数据归档脚本（企业知识库改造 阶段2：客服功能剥离前必须执行）

把 tickets / agent_presence / agent_shifts 三张表的数据导出为 JSON 存档，
配合 pg_dump（全库）与 Chroma 目录 tar（向量库）构成双重备份。

用法（backend 目录下）：
    python scripts/archive_tickets.py                # 导出到 backend/archive/<时间戳>/
    python scripts/archive_tickets.py --out 路径      # 指定输出目录

幂等安全：只读三张表 + 写新目录，不修改任何业务数据。
部署服务器上执行：docker compose exec backend python scripts/archive_tickets.py
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# 让脚本能 import app.*（backend 目录加入 sys.path）
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text

from app.infrastructure.database import async_session_factory

TABLES = ("tickets", "agent_presence", "agent_shifts")
# 各表排序键（表结构不同，没有统一的 created_at）
ORDER_BY = {
    "tickets": "created_at",
    "agent_presence": "last_seen_at",
    "agent_shifts": "updated_at",
}


async def _dump(table: str) -> list[dict]:
    async with async_session_factory() as db:
        result = await db.execute(text(f"SELECT * FROM {table} ORDER BY {ORDER_BY[table]}"))
        rows = result.fetchall()
        return [dict(row._mapping) for row in rows]


async def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for table in TABLES:
        rows = await _dump(table)
        file = out_dir / f"{table}.json"
        file.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"{table}: {len(rows)} 行 → {file}")
    print(f"\n归档完成，目录: {out_dir}")
    print("建议同时执行: pg_dump 全库备份 + Chroma 目录 tar（见阶段2方案）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="客服三表数据归档")
    default_out = (
        Path(__file__).resolve().parents[1]
        / "archive"
        / datetime.now().strftime("客服数据归档_%Y%m%d_%H%M%S")
    )
    parser.add_argument("--out", type=Path, default=default_out)
    args = parser.parse_args()
    asyncio.run(main(args.out))
