"""SSO 身份绑定模型（企业知识库改造 阶段4）

user_sso_identities：员工在第三方身份源（企业微信/钉钉/飞书…）的账号
与本地用户账号的绑定关系。同一员工可绑多个 provider（多通道并存），
同一 (provider, openid) 唯一——防止同一外部身份重复建号。
"""
import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base


class UserSSOIdentity(Base):
    __tablename__ = "user_sso_identities"
    __table_args__ = (
        UniqueConstraint("provider", "openid", name="uq_sso_provider_openid"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # wecom | dingtalk | test
    openid: Mapped[str] = mapped_column(String(128), nullable=False)  # provider 侧的用户唯一标识
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 绑定时的姓名快照
    department_names: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 绑定时的部门名快照
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # provider 原始返回（审计/排障）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
