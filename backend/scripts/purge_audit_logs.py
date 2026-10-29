"""审计日志保留策略脚本（产品化 C3）：只保留近 N 天，更早的物理删除。

用法（backend 目录下）：
    python scripts/purge_audit_logs.py --keep-days 90
加入每日定时任务（与备份脚本同一时刻，先备份后清理）。

审计日志只增不改不删是业务侧约定；此脚本是运维侧保留策略的物理执行——
建议清理前确保已按需导出归档（审计页"导出 CSV"）。
"""
import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

# 验收修复 A8：缺 sys.path 引导（直接 python scripts/purge_audit_logs.py 时
# ModuleNotFoundError: app）——与 eval_retrieval.py 统一写法
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import delete

from app.infrastructure.database import async_session_factory
from app.models.audit import AuditLog
from app.utils.time import tz_now


async def purge_old_logs(keep_days: int) -> int:
    """删除 created_at 早于 keep_days 天的审计日志，返回删除条数"""
    # tz_now() 与项目统一口径一致（UTC 服务器上 datetime.now() 会偏 8 小时）
    cutoff = tz_now() - timedelta(days=keep_days)
    async with async_session_factory() as db:
        result = await db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        await db.commit()
        return result.rowcount or 0


def main() -> None:
    parser = argparse.ArgumentParser(description="审计日志保留策略清理")
    parser.add_argument("--keep-days", type=int, default=90, help="保留最近 N 天（默认 90，最小 1）")
    args = parser.parse_args()
    if args.keep_days < 1:
        parser.error("--keep-days 最小为 1（负值会把全部审计日志删光）")
    removed = asyncio.run(purge_old_logs(args.keep_days))
    print(f"已删除 {removed} 条 {args.keep_days} 天前的审计日志")
    sys.exit(0)


if __name__ == "__main__":
    main()
