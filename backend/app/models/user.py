"""用户模型"""
import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")  # admin | editor | user（企业知识库改造阶段3）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 首登强制改密：管理员建号/批量导入时置 True，员工首次登录必须改密后才能提问
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # 顶号机制：每次登录 +1，令牌中携带的 sv 与当前值不一致即判定
    # "账号已在其他设备登录"。JWT 无状态，靠此版本号实现服务端单设备互踢。
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 关联
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="uploader", cascade="all, delete-orphan")
