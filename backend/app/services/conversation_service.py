"""会话管理服务"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload
from fastapi import HTTPException, status

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.feedback import MessageFeedback
from app.schemas.chat import ConversationDetail, ConversationResponse, FeedbackInfo


async def get_conversations(
    db: AsyncSession,
    user_id: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[ConversationResponse], int]:
    """获取用户的会话列表"""
    # 总数 (使用 count 避免加载全部数据)
    from sqlalchemy import func
    count_result = await db.execute(
        select(func.count(Conversation.id)).where(
            Conversation.user_id == user_id,
            Conversation.status == "active",
        )
    )
    total = count_result.scalar() or 0

    # 分页
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id, Conversation.status == "active")
        .order_by(desc(Conversation.updated_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    conversations = result.scalars().all()

    return (
        [ConversationResponse.model_validate(c) for c in conversations],
        total,
    )


async def get_conversation_detail(
    db: AsyncSession,
    user_id: str,
    conversation_id: str,
) -> ConversationDetail:
    """获取会话详情（含消息列表）。

    已删除（软删）的会话对用户不可见，与列表口径一致返回 404。
    """
    result = await db.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.status != "deleted",
        )
    )
    conv = result.scalar_one_or_none()

    if not conv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在",
        )

    detail = ConversationDetail.model_validate(conv)

    # 当前用户对各条消息的评价（一次 IN 查询批量取，防逐条 N+1）
    message_ids = [m.id for m in detail.messages]
    if message_ids:
        fb_result = await db.execute(
            select(MessageFeedback).where(
                MessageFeedback.user_id == user_id,
                MessageFeedback.message_id.in_(message_ids),
            )
        )
        feedback_by_message = {f.message_id: f for f in fb_result.scalars().all()}
        for msg in detail.messages:
            f = feedback_by_message.get(msg.id)
            if f:
                msg.feedback = FeedbackInfo(rating=f.rating, comment=f.comment)
    return detail


async def bump_message_count(
    db: AsyncSession,
    conversation_id: str,
    delta: int,
) -> None:
    """消息计数原子增减（SQL 端 message_count + delta，下限钳 0）。

    上线加固：Python 侧读-改-写（count += 1）在并发下会丢更新，
    导致计数与实际消息行数漂移；SQL 原子 UPDATE 由数据库保证串行。
    """
    from sqlalchemy import case, update

    await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(
            message_count=case(
                (Conversation.message_count + delta < 0, 0),
                else_=Conversation.message_count + delta,
            )
        )
    )


async def clear_conversations(
    db: AsyncSession,
    user_id: str,
) -> int:
    """一键清空：把该用户全部 active 会话软删除（status="deleted"）。

    阶段5 数据保留修复：软删除只让会话从用户列表消失，
    会话/消息/评价数据全部保留。返回删除数量。
    """
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id, Conversation.status == "active")
    )
    conversations = result.scalars().all()
    for conv in conversations:
        conv.status = "deleted"
    await db.commit()
    return len(conversations)


async def delete_conversation(
    db: AsyncSession,
    user_id: str,
    conversation_id: str,
) -> None:
    """软删除会话（status="deleted"），不做物理删除。

    阶段5 数据保留修复：用户"删除"只是让会话从自己的列表消失；
    会话/消息/评价全部保留——管理端的满意度统计不会因用户删除而丢失。
    """
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    conv = result.scalar_one_or_none()

    if not conv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在",
        )

    conv.status = "deleted"
    await db.commit()


async def update_conversation(
    db: AsyncSession,
    user_id: str,
    conversation_id: str,
    title: str | None = None,
    conv_status: str | None = None,
) -> ConversationResponse:
    """更新会话标题或状态。

    阶段5B 修复：状态白名单（active/archived）——外部不能传 deleted 或任意字符串
    把已删会话"复活"；已软删除的会话不可再被更新（与详情口径一致 404）。
    """
    if conv_status is not None and conv_status not in ("active", "archived"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法会话状态",
        )

    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.status != "deleted",
        )
    )
    conv = result.scalar_one_or_none()

    if not conv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在",
        )

    if title is not None:
        conv.title = title
    if conv_status is not None:
        conv.status = conv_status

    await db.flush()
    await db.refresh(conv)
    return ConversationResponse.model_validate(conv)
