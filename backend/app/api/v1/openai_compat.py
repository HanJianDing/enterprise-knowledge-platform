"""OpenAI 兼容对话 API（批次11e）——通用 LLM 客户端接入门

/v1/chat/completions：接收 OpenAI 协议（messages/stream），适配到内部
知识库问答链路。任何按 OpenAI 协议写的客户端（Dify/n8n/Coze/LangChain/
自研脚本）一行代码不改即可把本系统当作"带知识库的 LLM"调用。

鉴权：Authorization: Bearer <api_key>（SHA-256 哈希校验；
key 的可见库范围=所属用户的权限——权限穿透，与员工端同口径）。
"""
import hashlib
import logging

logger = logging.getLogger(__name__)
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.config import settings
from app.models.api_key import ApiKey
from app.models.user import User

router = APIRouter(prefix="/v1", tags=["OpenAI 兼容"])


class ChatMessage(BaseModel):
    role: str  # system/user/assistant
    # 批次A 质检 P2：单条消息限长 2000 字（与员工端"上线安全修复"同口径）——
    # key 持有者绕过员工端直接投递超长文本时，双 LLM 调用全额计费的滥用面
    content: str = Field(max_length=2000)


class ChatCompletionRequest(BaseModel):
    model: str = "rag-knowledge-base"  # 任意值，实际走内部 RAG（兼容客户端必填）
    # 批次A 质检 P2：消息条数上限 20（超长历史进 LLM 上下文烧成本且
    # 内部历史消费只取最近若干条，多投无用）
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)
    stream: bool = False
    temperature: float | None = None


def _hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


async def _auth_by_api_key(request: Request, db: AsyncSession) -> tuple[User, ApiKey]:
    """Bearer key 鉴权：返回 (key 所属用户, key)。
    可见库范围=该用户权限（权限穿透）。附 key 维度限频（P1-3）：
    key 泄露时不会无限烧 LLM 配额（每分钟 20 次，与员工端同量级）。"""
    from app.middleware.rate_limit import _limiter

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing API key")
    plain = auth[7:].strip()
    key_hash = _hash_key(plain)
    # 质检 P2：尊重限频总开关（测试/CI 关闭 rate_limit_enabled 时不受限）
    if settings.rate_limit_enabled and not _limiter.allow(f"api:{key_hash}", 20):
        raise HTTPException(status_code=429, detail="Too many requests")
    result = await db.execute(
        select(ApiKey, User)
        .join(User, User.id == ApiKey.user_id)
        .where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True))
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    key, user = row
    if key.expires_at:
        expires = key.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)  # S10：naive/aware 统一
        if expires < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="API key expired")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="User disabled")
    key.last_used_at = datetime.now(timezone.utc)
    await db.commit()
    return user, key


def _extract_question(messages: list[ChatMessage]) -> tuple[str, list[dict]]:
    """OpenAI messages → (当前问题, 历史对话)。

    批次A·11e backlog 多轮接入：取最后一条 user 消息为当前问题，
    其余 user/assistant 消息按序转为内部 chat_history 格式
    （[{"role","content"}...]）注入问答链路——检索词扩展与 LLM
    上下文都吃客户端自带历史；system 消息是客户端角色设定，
    内部链路不消费，忽略。
    """
    msgs = [m for m in messages if m.role in ("user", "assistant")]
    question = ""
    history: list[dict] = []
    for m in reversed(msgs):
        if m.role == "user" and not question:
            question = m.content.strip()
            continue
        history.insert(0, {"role": m.role, "content": m.content})
    return question, history


def _completion_payload(content: str, usage: dict | None = None) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "rag-knowledge-base",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@router.post("/chat/completions")
async def chat_completions(
    data: ChatCompletionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OpenAI 协议对话：messages→内部问答（非流式/流式双模式）"""
    user, api_key = await _auth_by_api_key(request, db)
    question, history = _extract_question(data.messages)
    if not question:
        raise HTTPException(status_code=400, detail="messages 中没有 user 消息")

    # 审计（OpenAI 门调用留痕；烧 LLM 成本可追溯）
    from app.services import audit_service

    async def _audit(content: str, stream: bool) -> None:
        try:
            await audit_service.record_audit(
                db, user.id, user.username, "api.openai_chat",
                resource_type="api_key", resource_id=api_key.id,
                detail={"model": data.model, "stream": stream,
                        "key_name": api_key.name,
                        "question_len": len(question),
                        "answer_len": len(content)},
                request=request,
            )
            await db.commit()
        except Exception as e:
            logger.warning("API 调用审计失败: %s", e)  # 审计失败可见但不阻断

    # 内部问答：复用 stream_chat 的 SSE 流，抽取权威答案文本
    from app.services import chat_service

    async def _run_rag(conversation_id: str | None) -> tuple[str, dict | None]:
        collected = {"content": "", "token_count": 0, "trace_id": None,
                     "token_usage": None}
        async for event in chat_service.stream_chat(
            db, user, conversation_id, question, "user",
            external_history=history, capture_usage=True,
            # 批次F 挂清单 #12：key 库白名单——检索范围收敛到
            # 白名单库（∩ 账号可见范围），null=穿透账号权限
            kb_restrict=api_key.kb_ids,
        ):
            if "text_delta" in event:
                try:
                    payload = json.loads(event.split("data: ", 1)[1])
                    collected["content"] += payload.get("content", "")
                except Exception:
                    pass
            elif '"type": "done"' in event:
                try:
                    payload = json.loads(event.split("data: ", 1)[1])
                    collected["content"] = payload.get("content") or collected["content"]
                    collected["token_count"] = payload.get("token_count", 0)
                    collected["trace_id"] = payload.get("trace_id")
                    collected["token_usage"] = payload.get("token_usage")
                except Exception:
                    pass
        return collected["content"], collected

    if data.stream:
        async def generate():
            # OpenAI 流式格式：data: {...delta...}\n\n 结尾 data: [DONE]；
            # 质检 P3：RAG 失败（无任何内容产出）时以 error chunk 收尾、
            # 不发 [DONE]——客户端按无 [DONE] 判失败，不伪装成功
            produced = False
            async for event in chat_service.stream_chat(
                db, user, None, question, "user", external_history=history,
                # 质检 P1-1：流式分支同样透传白名单——此前漏传导致
                # 带库白名单的 key 走流式时检索范围退化为账号全部可见库
                kb_restrict=api_key.kb_ids,
            ):
                if "text_delta" in event:
                    try:
                        payload = json.loads(event.split("data: ", 1)[1])
                        chunk = payload.get("content", "")
                        if chunk:
                            produced = True
                            yield "data: " + json.dumps({
                                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "rag-knowledge-base",
                                "choices": [{"index": 0,
                                             "delta": {"content": chunk},
                                             "finish_reason": None}],
                            }, ensure_ascii=False) + "\n\n"
                    except Exception:
                        continue
            if produced:
                yield "data: [DONE]\n\n"
            else:
                yield "data: " + json.dumps({
                    "error": {"message": "知识库问答生成失败，请稍后重试",
                              "type": "rag_error"},
                }, ensure_ascii=False) + "\n\n"
            await _audit("(streamed)", produced)

        return StreamingResponse(generate(), media_type="text/event-stream")

    content, meta = await _run_rag(None)
    if not content.strip():
        # S5：RAG 失败不得伪装成功——OpenAI 客户端按非 200 判错
        raise HTTPException(status_code=502, detail="知识库问答生成失败，请稍后重试")
    await _audit(content, False)
    # 批次A·11e backlog：usage 真实统计——优先用内部链路的真实 token 用量
    # （capture_usage 捕获），取不到时回退字符数估算（不阻塞主流程）。
    # 口径说明：token_usage 仅覆盖生成阶段（prompt 的检索上下文 token
    # 未单独统计，prompt_tokens 可能为 0——诚实返回所捕获到的数据）。
    # 批次B P3：数值转 int 加防护（上游异常类型时回退 0，不 500）
    def _safe_int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    tu = (meta or {}).get("token_usage") or {}
    usage = {
        "prompt_tokens": _safe_int(tu.get("prompt_tokens") or 0),
        "completion_tokens": _safe_int(
            tu.get("completion_tokens")
            or tu.get("output_tokens")
            or (meta or {}).get("token_count") or 0
        ),
        "total_tokens": _safe_int(
            tu.get("total_tokens") or (meta or {}).get("token_count") or 0
        ),
    }
    return _completion_payload(content, usage)
