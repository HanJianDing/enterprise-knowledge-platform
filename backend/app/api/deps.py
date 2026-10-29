"""API 依赖注入"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import get_db
from app.middleware.auth import get_current_user, require_admin, require_audit_view
from app.models.user import User

# 重新导出，方便路由使用
__all__ = ["get_db", "get_current_user", "require_admin", "require_audit_view"]
