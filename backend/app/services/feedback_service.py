"""消息评价服务 — 点赞/点踩的幂等写入与取消"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.feedback import MessageFeedback
from app.models.message import Message


async def upsert_feedback(
    db: AsyncSession,
    user_id: str,
    message_id: str,
    rating: str,
    comment: str | None,
) -> MessageFeedback:
    """幂等写入评价：已有则更新（改评价/补意见），没有则创建。

    校验：
    - 消息必须属于当前用户的会话（否则 404，防越权评价他人消息）
    - 只能评价 AI 回答（assistant），system/agent 等角色 400
    """
    result = await db.execute(
        select(Message)
        .options(selectinload(Message.conversation))
        .where(Message.id == message_id)
    )
    msg = result.scalar_one_or_none()
    if not msg or msg.conversation.user_id != user_id:
        raise HTTPException(status_code=404, detail="消息不存在")
    if msg.role != "assistant":
        raise HTTPException(status_code=400, detail="只能评价 AI 回答")

    result = await db.execute(
        select(MessageFeedback).where(
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == user_id,
        )
    )
    feedback = result.scalar_one_or_none()
    # 语义收紧：只有点踩才可能有意见，点赞强制清空 comment，
    # 保证数据库里永远不会出现"点赞带意见"的脏数据
    if rating == "like":
        comment = None
    if feedback:
        feedback.rating = rating
        feedback.comment = comment
    else:
        feedback = MessageFeedback(
            message_id=message_id, user_id=user_id, rating=rating, comment=comment
        )
        db.add(feedback)
    await db.commit()
    await db.refresh(feedback)
    return feedback


async def delete_feedback(
    db: AsyncSession,
    user_id: str,
    message_id: str,
) -> None:
    """取消评价（幂等：没有评价时静默成功，前端"再点一次取消"无需区分状态）"""
    result = await db.execute(
        select(MessageFeedback).where(
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == user_id,
        )
    )
    feedback = result.scalar_one_or_none()
    if not feedback:
        return
    await db.delete(feedback)
    await db.commit()
