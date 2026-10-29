"""MCP 接入凭证模型（批次D：外部接入管理页——"发钥匙的柜台"）

与登录 JWT 完全不同的生命周期：
- 长期有效（登录 token 15 分钟过期，集成场景不可用）
- 不受"顶号踢出"影响（管理员的会话版本变化不牵连集成凭证）
- 撤销即失效（独立吊销开关）；库内存哈希，原文只在创建时展示一次
"""
import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, ForeignKey, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base


class McpCredential(Base):
    __tablename__ = "mcp_credentials"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)  # 接入方标识（如"OA系统"）
    # 凭证原文的 sha256 哈希——原文只在创建响应中展示一次，库里不可逆。
    # 随机 192 位熵的凭证无需 bcrypt 慢哈希（与口令的弱熵场景不同），sha256 即可
    token_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    # 绑定一个真实账号：调用时的权限=该账号可见范围 ∩ kb_ids 白名单
    owner_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    # 可见知识库白名单（null=不限，按绑定账号权限兜底）
    kb_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
