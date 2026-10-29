"""消息评价 API — 用户对 AI 回答的点赞/点踩"""
from fastapi import APIRouter, Depends

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.schemas.chat import FeedbackInfo, FeedbackSubmit
from app.services import feedback_service

router = APIRouter(prefix="/api/v1/messages", tags=["反馈"])


@router.put("/{message_id}/feedback", response_model=FeedbackInfo)
async def submit_feedback(
    message_id: str,
    data: FeedbackSubmit,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """提交/修改评价（幂等：再点同侧=改意见，点另一侧=改评价）"""
    feedback = await feedback_service.upsert_feedback(
        db, current_user.id, message_id, data.rating, data.comment
    )
    return FeedbackInfo(rating=feedback.rating, comment=feedback.comment)


@router.delete("/{message_id}/feedback")
async def cancel_feedback(
    message_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """取消评价（幂等：未评价时静默成功）"""
    await feedback_service.delete_feedback(db, current_user.id, message_id)
    return {"message": "评价已取消"}
