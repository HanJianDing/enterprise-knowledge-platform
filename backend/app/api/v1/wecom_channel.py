"""企业微信对话通道（批次E·6e）——员工在企微里直接问知识库

企微自建应用"接收消息"回调：企微服务器把员工消息 POST 到本端点
（AES 加密+签名），本服务解密 → 后台问答 → 应用消息 API 主动推送
（被动回复受 5 秒窗口限制，完整问答链路必须异步推送）。
通道启用条件：WECOM 三项配置齐全（corp_id/agent_id/secret，SSO 时代
已预留）+ WECOM_CHANNEL_TOKEN + WECOM_CHANNEL_AES_KEY 配置——
未配置时端点返回 503（通道未启用）；等企微账号启用后填配置即开。
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wecom", tags=["企微通道"])

# 后台推送任务集合（批次E 质检 C：保存引用防 GC 回收 + 异常消费防噪音）
_push_tasks: set = set()


def _check_timestamp_window(timestamp: str) -> None:
    """重放防护（批次E 质检）：timestamp 必须在 ±5 分钟窗口内——
    配合 MsgId 排重，双重防重放（官方对接指引建议的时间窗校验）"""
    import time

    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="时间戳非法")
    if abs(time.time() - ts) > 300:
        raise HTTPException(status_code=403, detail="时间戳超出有效窗口")


def _channel_ready() -> tuple[str, str] | None:
    """通道配置齐全返回 (token, aes_key)；未配置返回 None。
    批次E 质检补审：推送链路硬依赖的全部配置纳入启用条件——corp_id
    （解密尾缀校验）、wecom_secret（access_token）、wecom_agent_id
    （消息推送目标应用）任一缺失即通道关闭，防"已启用但推送静默失败"
    """
    token = settings.wecom_channel_token
    aes_key = settings.wecom_channel_aes_key
    if (not token or not aes_key or not settings.wecom_corp_id
            or not settings.wecom_secret or not settings.wecom_agent_id):
        return None
    return token, aes_key


@router.get("/callback")
async def wecom_verify(request: Request):
    """URL 验证（企微首次配置回调时 GET）：验签 → 解密 echostr 原样返回"""
    ready = _channel_ready()
    if ready is None:
        raise HTTPException(status_code=503, detail="企微通道未启用")
    token, aes_key = ready

    from app.services.wecom_crypto import decrypt_message, sign

    params = request.query_params
    echostr = params.get("echostr", "")
    if not echostr:
        raise HTTPException(status_code=400, detail="缺少 echostr")
    # 测试模式：跳过签名校验（本地联调用；生产必须关）
    if not settings.wecom_channel_test_mode:
        _check_timestamp_window(params.get("timestamp", ""))
        expected = sign(token, params.get("timestamp", ""),
                        params.get("nonce", ""), echostr)
        if params.get("msg_signature", "") != expected:
            raise HTTPException(status_code=403, detail="签名校验失败")
    try:
        plain = decrypt_message(echostr, aes_key, settings.wecom_corp_id or None)
    except Exception as e:
        logger.warning("echostr 解密失败: %s", e)
        raise HTTPException(status_code=400, detail="echostr 解密失败")
    return PlainTextResponse(plain)


@router.post("/callback")
async def wecom_message(request: Request):
    """接收员工消息：验签 → 解密 → 空响应即回（企微 5 秒窗口）→ 后台问答 → 应用消息 API 主动推送"""
    ready = _channel_ready()
    if ready is None:
        raise HTTPException(status_code=503, detail="企微通道未启用")
    token, aes_key = ready

    from app.services.wecom_crypto import decrypt_message, extract_text_message, sign

    body = await request.body()
    params = request.query_params
    if not settings.wecom_channel_test_mode:
        _check_timestamp_window(params.get("timestamp", ""))
        import xml.etree.ElementTree as ET

        root = ET.fromstring(body.decode("utf-8"))
        encrypt = root.findtext("Encrypt", "")
        expected = sign(token, params.get("timestamp", ""),
                        params.get("nonce", ""), encrypt)
        if params.get("msg_signature", "") != expected:
            raise HTTPException(status_code=403, detail="签名校验失败")
    else:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(body.decode("utf-8"))
        encrypt = root.findtext("Encrypt", "")

    try:
        plain_xml = decrypt_message(encrypt, aes_key,
                                    settings.wecom_corp_id or None)
    except Exception as e:
        logger.warning("消息解密失败: %s", e)
        raise HTTPException(status_code=400, detail="消息解密失败")

    msg = extract_text_message(plain_xml)
    # 批次E 质检修复（5 秒窗口，启用前必修）：同步问答 10~60s 远超企微
    # 5 秒响应窗口（用户收不到回复+企微重试 3 次=3 倍重复消耗）——回调
    # 立即回空响应（企微视为已接收），问答在后台任务完成后经应用消息
    # API 主动推送；MsgId 排重防重试风暴
    import asyncio

    from app.services.wecom_channel_bridge import (
        dedupe_and_mark, push_text_message,
    )

    if msg is None:
        return PlainTextResponse("", media_type="text/plain")
    if not dedupe_and_mark(msg["msg_id"]):
        # 重复消息（企微重试）：不回不推，静默丢弃
        return PlainTextResponse("", media_type="text/plain")

    _push_task = asyncio.create_task(
        _answer_and_push(msg["content"], msg["from_user"])
    )
    _push_tasks.add(_push_task)
    _push_task.add_done_callback(_push_tasks.discard)
    return PlainTextResponse("", media_type="text/plain")


async def _answer_and_push(question: str, from_user: str) -> None:
    """后台任务：内部问答 → 字节级截断 → 应用消息 API 主动推送。

    用户绑定机制（企微 UserID ↔ 系统账号映射）随企微账号启用时补齐——
    当前骨架用 admin 演示账号（通道演示语义）。
    """
    from sqlalchemy import select

    from app.infrastructure.database import async_session_factory
    from app.models.user import User
    from app.services import chat_service
    from app.services.wecom_channel_bridge import push_text_message

    try:
        async with async_session_factory() as db:
            user = (await db.execute(
                select(User).where(User.role == "admin").limit(1)
            )).scalars().first()
            if user is None:
                answer = "系统未初始化账号，请联系管理员"
            else:
                chunks: list[str] = []
                async for event in chat_service.stream_chat(
                    db, user, None, question, "user"
                ):
                    if '"type": "done"' in event:
                        import json

                        try:
                            payload = json.loads(event.split("data: ", 1)[1])
                            content = payload.get("content")
                            if content:
                                chunks = [content]
                        except Exception as e:
                            logger.warning("done 事件解析失败: %s", e)
                answer = chunks[0] if chunks else "抱歉，暂时无法回答这个问题。"
        from app.services.wecom_channel_bridge import truncate_bytes

        # 批次E 质检修复：按**字节**截断（企微限制约 2048 字节；此前按
        # 字符截断——中文 1800 字 ≈ 5400 字节，保护失效）
        answer = truncate_bytes(answer, 1800)
        await push_text_message(from_user, answer)
    except Exception as e:
        logger.warning("企微后台问答失败（用户无回复）: %s", e)
