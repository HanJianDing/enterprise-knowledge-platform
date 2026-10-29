"""对话 API - SSE 流式问答"""
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.api.deps import get_db, get_current_user
from app.middleware.rate_limit import chat_rate_limit
from app.models.user import User
from app.schemas.chat import ChatRequest, RegenerateRequest
from app.services import chat_service, setting_service

router = APIRouter(prefix="/api/v1/chat", tags=["对话"])


def _single_consumer(gen: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
    """批次5 压测防御：单消费者守卫——压测下出现 "asynchronous
    generator is already running" 500（客户端中断后的异常路径可能
    触发同一生成器二次迭代）；重入时静默结束不崩溃。"""
    iterating = False

    async def wrapped():
        nonlocal iterating
        if iterating:
            return  # 重入：立即结束（不抛异常、不二次迭代）
        iterating = True
        try:
            async for item in gen:
                yield item
        finally:
            iterating = False

    return wrapped()


@router.post("/stream")
async def chat_stream(
    data: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(chat_rate_limit),
):
    """SSE 流式对话（同一用户每分钟最多 10 次，防脚本刷 LLM 计费）

    企业知识库改造阶段3：限频按登录用户计数（企业出口 IP 收敛时按 IP
    会误伤全公司）；未带 token 的请求在鉴权处 401 短路、不会触发任何
    LLM 计费，无需计数。开放注册已关闭，账号只由管理员创建/导入。
    """
    if not data.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    # 首登强制改密：未改密前不允许提问（管理员建号/批量导入的账号）
    if current_user.must_change_password:
        raise HTTPException(status_code=403, detail="首次登录请先修改初始密码")

    # 角色校验：非 admin 用户请求 admin 回答模式时降级为 user（防伪造）
    effective_role = chat_service.resolve_effective_role(data.role, current_user.role)
    if effective_role != data.role:
        logger.warning(
            f"非管理员请求 admin 回答模式，已降级为 user: "
            f"user_id={current_user.id}, username={current_user.username}"
        )

    # 批次4：SSE 生成器在响应阶段执行、请求 span 上下文已退出——
    # 必须在请求处理阶段（span 活跃期）捕获 trace_id 经闭包传入
    from app.observability import get_current_trace_id

    request_trace_id = get_current_trace_id()

    async def generate():
        try:
            async for event in _single_consumer(chat_service.stream_chat(
                db, current_user, data.conversation_id, data.message, effective_role,
                trace_id=request_trace_id,
            )):
                yield event
        finally:
            # 批次F 验收（挂清单收敛）：客户端断流时生成器被取消，
            # get_db 依赖清理在取消路径可能不执行——显式 rollback 释放
            # 事务（真实验收：僵尸事务持行锁数小时，清空对话全部排队）
            try:
                await db.rollback()
            except Exception:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/regenerate")
async def chat_regenerate(
    data: RegenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(chat_rate_limit),
):
    """重新生成最后一条回答（SSE 事件序列与 /stream 一致，同样限频）

    校验 + 旧回答删除在流式开始前完成：404/400 等错误才能以 JSON 正常返回，
    而不是流开始后中断连接。
    """
    if current_user.must_change_password:
        raise HTTPException(status_code=403, detail="首次登录请先修改初始密码")

    effective_role = chat_service.resolve_effective_role(data.role, current_user.role)
    message_text, exclude_id = await chat_service.prepare_regenerate(
        db, current_user, data.conversation_id
    )

    from app.observability import get_current_trace_id

    request_trace_id = get_current_trace_id()

    async def generate():
        try:
            async for event in _single_consumer(chat_service.regenerate_answer(
                db, current_user, data.conversation_id, message_text, exclude_id,
                effective_role, trace_id=request_trace_id,
            )):
                yield event
        finally:
            # 批次F 验收（挂清单收敛）：与 chat_stream 同款——断流取消
            # 路径显式 rollback，防僵尸事务持锁
            try:
                await db.rollback()
            except Exception:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/quick-questions")
async def get_quick_questions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """欢迎页快捷问题（登录即可读；管理端 PUT /admin/settings/quick-questions 修改）"""
    questions = await setting_service.get_quick_questions(db)
    return {"questions": questions}
