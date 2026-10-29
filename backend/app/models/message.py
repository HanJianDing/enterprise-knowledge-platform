"""消息模型"""
import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant | system | agent(人工客服回复)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 批次4 可观测（质检 N2）：trace_id 持久化——主路径刷新会话后
    # trace 链接仍可见（否则仅流式回退路径能看到）
    trace_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # 关联
    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    # 该消息收到的用户评价（消息删除时随 ORM 级联清理，防孤儿评价）
    feedbacks: Mapped[list["MessageFeedback"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
