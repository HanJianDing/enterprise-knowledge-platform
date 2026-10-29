"""消息评价（点赞/点踩）模型"""
import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base


class MessageFeedback(Base):
    """用户对 AI 回答的点赞/点踩（点踩可带意见）

    同一用户对同一条消息最多一条评价：
    UniqueConstraint 是数据库层兜底（防并发双击产生两条），
    API 层用 upsert 保证幂等。
    """
    __tablename__ = "message_feedbacks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    rating: Mapped[str] = mapped_column(String(10), nullable=False)  # like | dislike
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)  # 点踩意见（可选）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_feedback_message_user"),
    )

    # 关联（异步会话必须 selectinload，防 MissingGreenlet）
    message: Mapped["Message"] = relationship(back_populates="feedbacks")
    user: Mapped["User"] = relationship()
