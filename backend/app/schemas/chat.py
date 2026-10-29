"""对话相关 Schema"""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    conversation_id: str | None = Field(None, description="会话ID，为空则创建新会话")
    # 上线安全修复：限制消息长度（防粘贴数 MB 文本一次烧掉巨额 LLM token 成本）
    message: str = Field(..., min_length=1, max_length=2000)
    stream: bool = Field(default=True)
    role: Literal["user", "admin"] = Field(
        default="user",
        description="回答角色：user=员工（结论先行/限长/带引用），admin=管理员（完整回答/引用/调试信息）",
    )


class FeedbackInfo(BaseModel):
    """当前用户对某条消息的评价（会话详情按条返回）"""
    rating: Literal["like", "dislike"]
    comment: str | None = None


class FeedbackSubmit(BaseModel):
    rating: Literal["like", "dislike"]
    comment: str | None = Field(None, max_length=500, description="点踩意见（可选）")


class ChatMessageResponse(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str
    citations: list[dict] | None = None
    token_count: int | None = None
    latency_ms: int | None = None
    feedback: FeedbackInfo | None = None  # 当前用户对该消息的评价（未评价为 null）
    trace_id: str | None = None  # 批次4 可观测（质检 N2）：主路径刷新后 trace 链接可见
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationResponse(BaseModel):
    id: str
    title: str
    status: str
    message_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetail(BaseModel):
    id: str
    title: str
    status: str
    message_count: int
    messages: list[ChatMessageResponse] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationUpdate(BaseModel):
    title: str | None = None
    status: str | None = None  # active | archived


class RegenerateRequest(BaseModel):
    conversation_id: str
    role: Literal["user", "admin"] = Field(default="user")


class QuickQuestionsUpdate(BaseModel):
    """管理端更新欢迎页快捷问题（条数/长度校验在端点层）"""
    questions: list[str] = Field(..., min_length=1)
