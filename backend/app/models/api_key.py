"""API key 模型（批次11e OpenAI 兼容 API）——通用 LLM 客户端接入凭据"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base


class ApiKey(Base):
    """API key：SHA-256 哈希存储，明文只在生成时返回一次；
    关联用户——key 的知识库可见范围=该用户权限（权限穿透）。
    批次F 挂清单 #12：kb_ids 库白名单（null=穿透账号权限，
    非 null=仅限白名单库——与账号可见范围取交集）"""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    key_prefix: Mapped[str] = mapped_column(String(20))  # 展示用（"sk-rag-"+8位hex）
    name: Mapped[str] = mapped_column(String(100))  # 用途备注（如 "Dify 工作流"）
    kb_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 库白名单
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
