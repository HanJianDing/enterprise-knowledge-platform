"""会话管理 API"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.schemas.chat import (
    ConversationResponse, ConversationDetail, ConversationUpdate,
)
from app.schemas.common import MessageResponse, PaginatedResponse
from app.services import conversation_service

router = APIRouter(prefix="/api/v1/conversations", tags=["会话"])


@router.get("", response_model=PaginatedResponse)
async def list_conversations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取会话列表"""
    conversations, total = await conversation_service.get_conversations(
        db, current_user.id, page, page_size
    )
    return PaginatedResponse(
        items=conversations,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取会话详情（含消息历史）"""
    return await conversation_service.get_conversation_detail(
        db, current_user.id, conversation_id
    )


@router.post("/clear")
async def clear_conversations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一键清空全部历史对话（软删除，数据保留）"""
    deleted = await conversation_service.clear_conversations(db, current_user.id)
    return {"deleted": deleted, "skipped": 0, "message": f"已删除 {deleted} 个会话"}


@router.delete("/{conversation_id}", response_model=MessageResponse)
async def delete_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除会话"""
    await conversation_service.delete_conversation(
        db, current_user.id, conversation_id
    )
    return MessageResponse(message="会话已删除")


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: str,
    data: ConversationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新会话（标题/归档）"""
    return await conversation_service.update_conversation(
        db, current_user.id, conversation_id,
        title=data.title, conv_status=data.status,
    )
