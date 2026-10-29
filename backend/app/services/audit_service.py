"""审计日志服务（企业知识库改造 阶段3）

在关键写操作处显式埋点（登录/建号/改角色/改权限/传删文档/分类/部门等）。
审计是"事实记录"：只增不改不删。

写入方式：与业务**同一会话**直接 db.add（不单独 flush/commit）——
- add 只进 identity map、不发 SQL，不会失败也不会污染会话，
  "审计失败不影响业务"天然成立；
- 审计随业务事务一起提交/回滚：只有真正落库的操作才留痕（语义更正确）；
- 没有后台任务 → 没有跨请求会话/连接争用（SQLite 单写者下尤其重要），
  也不存在任务跨事件循环存活的坑（曾导致测试大面积连锁失败）。
"""
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User


async def record_audit(
    db: AsyncSession,
    user_id: str,
    username: str,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail: dict | None = None,
    request: Request | None = None,
) -> None:
    """在当前请求会话内追加一条审计日志（随业务事务一起提交）。

    注意 user_id/username 传原始值而非 ORM 对象——请求中途发生过
    rollback（如 CSV 导入的行级回滚）会让已加载的 User 对象过期，
    之后再读 user.id 会触发数据库刷新（MissingGreenlet）。
    """
    ip = None
    user_agent = None
    if request is not None:
        forwarded = request.headers.get("x-forwarded-for")
        ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else None
        )
        user_agent = (request.headers.get("user-agent") or "")[:500] or None
    db.add(AuditLog(
        user_id=user_id,
        username=username,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
        ip=ip,
        user_agent=user_agent,
    ))
