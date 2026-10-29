"""阶段2 聊天端新功能测试 — 重新生成 / 一键清空 / 快捷问题

SSE 集成沿用 test_role_response 的 mock 链路（不调用真实 LLM）。
"""
import json
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tests.conftest import create_user_in_db
from app.infrastructure.database import async_session_factory
from app.models.conversation import Conversation
from app.models.message import Message
from tests.test_role_response import _install_chat_mocks, FailingChain


async def _collect_sse(client: AsyncClient, url: str, payload: dict) -> dict[str, list[dict]]:
    """发送流式请求并解析 SSE 事件，返回 {事件名: [data列表]}"""
    events: dict[str, list[dict]] = {}
    async with client.stream("POST", url, json=payload) as resp:
        assert resp.status_code == 200
        event_name = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: ") and event_name:
                events.setdefault(event_name, []).append(json.loads(line[len("data: "):]))
    return events


async def _ask_once(client: AsyncClient, question: str = "iPhone 16 性能怎么样") -> dict:
    """mock 链路下问一轮，返回会话 id（不校验内容）"""
    events = await _collect_sse(client, "/api/v1/chat/stream", {"message": question})
    return events["done"][-1]["conversation_id"]


async def _conversation_state(client: AsyncClient, conv_id: str) -> dict:
    """会话详情（含 message_count 与消息列表）"""
    return (await client.get(f"/api/v1/conversations/{conv_id}")).json()


async def _current_user_id(client: AsyncClient) -> str:
    return (await client.get("/api/v1/auth/me")).json()["id"]


# ════════════════════════════════════════════════════
# 重新生成回答
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_regenerate_replaces_last_answer(user_client: AsyncClient):
    """正常流：重答后旧回答被替换（仍是 1 问 1 答）、计数一致、历史排除被重答的问题"""
    patches = _install_chat_mocks(["旧回答：参考L码。"])
    for p in patches:
        p.start()
    try:
        conv_id = await _ask_once(user_client)
        detail = await _conversation_state(user_client, conv_id)
        user_msg_id = next(m["id"] for m in detail["messages"] if m["role"] == "user")
        assert detail["message_count"] == 2

        # 换一批假输出，模拟"重新生成的答案不同"
        for p in patches:
            p.stop()
        patches2 = _install_chat_mocks(["新回答：建议XL码。"])
        for p in patches2:
            p.start()
        try:
            # 监听 _get_chat_history 的 exclude_message_id，验证被重答的问题被排除出历史
            captured: dict = {}
            import app.services.chat_service as chat_service
            real = chat_service._get_chat_history

            async def spy(db, conversation_id, exclude_message_id=None):
                captured["exclude"] = exclude_message_id
                return await real(db, conversation_id, exclude_message_id=exclude_message_id)

            spy_patch = patch("app.services.chat_service._get_chat_history", side_effect=spy)
            spy_patch.start()
            try:
                events = await _collect_sse(
                    user_client, "/api/v1/chat/regenerate",
                    {"conversation_id": conv_id},
                )
            finally:
                spy_patch.stop()

            done = events["done"][-1]
            assert done["conversation_id"] == conv_id
            assert done["content"] == "新回答：建议XL码。"
            assert captured["exclude"] == user_msg_id

            # 库状态：仍 1 问 1 答、计数与实际行数一致、旧回答已被替换
            detail = await _conversation_state(user_client, conv_id)
            assistant_msgs = [m for m in detail["messages"] if m["role"] == "assistant"]
            assert len(assistant_msgs) == 1
            assert assistant_msgs[0]["content"] == "新回答：建议XL码。"
            assert detail["message_count"] == 2
        finally:
            for p in patches2:
                p.stop()
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_regenerate_no_answer_yet_rejected(user_client: AsyncClient):
    """最后一条 user 消息之后没有 AI 回答（LLM 失败场景）→ 400"""
    classify_patch, retrieve_patch, chain_patch, categories_patch, rerank_patch, \
        visible_kb_patch, visible_docs_patch, chunk_count_patch = _install_chat_mocks([])
    chain_patch = patch(
        "app.services.chat_service.build_rag_chain",
        return_value=FailingChain(),
    )
    patches = [
        classify_patch, retrieve_patch, chain_patch, categories_patch,
        rerank_patch, visible_kb_patch, visible_docs_patch, chunk_count_patch,
    ]
    for p in patches:
        p.start()
    try:
        await _collect_sse(user_client, "/api/v1/chat/stream", {"message": "iPhone 16 性能怎么样"})
        resp = await user_client.get("/api/v1/conversations")
        conv_id = resp.json()["items"][0]["id"]
        resp = await user_client.post("/api/v1/chat/regenerate", json={"conversation_id": conv_id})
        assert resp.status_code == 400
        assert "尚未回答完毕" in resp.json()["detail"]
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_regenerate_requires_auth(client: AsyncClient):
    """未认证 401"""
    resp = await client.post("/api/v1/chat/regenerate", json={"conversation_id": "x"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_regenerate_non_owner_404(user_client: AsyncClient, client: AsyncClient):
    """别人的会话 → 404"""
    patches = _install_chat_mocks(["旧回答。"])
    for p in patches:
        p.start()
    try:
        conv_id = await _ask_once(user_client)

        await create_user_in_db("other_user", "other@test.com", "123456")
        resp = await client.post("/api/v1/auth/login", json={
            "username": "other_user", "password": "123456"
        })
        client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

        resp = await client.post("/api/v1/chat/regenerate", json={"conversation_id": conv_id})
        assert resp.status_code == 404
    finally:
        for p in patches:
            p.stop()
        client.headers.pop("Authorization", None)


# ════════════════════════════════════════════════════
# 一键清空
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_clear_deletes_all_active_conversations(user_client: AsyncClient):
    """清空：active 会话全删、列表为空"""
    patches = _install_chat_mocks(["回答1。"])
    for p in patches:
        p.start()
    try:
        await _ask_once(user_client, "第一个问题")
        await _ask_once(user_client, "第二个问题")

        resp = await user_client.post("/api/v1/conversations/clear")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 2
        assert data["skipped"] == 0

        resp = await user_client.get("/api/v1/conversations")
        assert resp.json()["total"] == 0
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_clear_deletes_all_including_system_messages(user_client: AsyncClient):
    """企业改造阶段2（客服剥离后）：清空不再跳过任何会话——全部软删除"""
    patches = _install_chat_mocks(["回答1。"])
    for p in patches:
        p.start()
    try:
        await _ask_once(user_client, "第一个问题")

        resp = await user_client.post("/api/v1/conversations/clear")
        data = resp.json()
        assert data["deleted"] == 1
        assert data["skipped"] == 0

        resp = await user_client.get("/api/v1/conversations")
        assert resp.json()["total"] == 0
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_clear_is_per_user(user_client: AsyncClient, client: AsyncClient):
    """清空只影响本人会话"""
    # 注意：user_client 与 client 是同一个 AsyncClient 实例（conftest 共享），
    # 换 token 前先记住用户A的，最后恢复
    auth_a = user_client.headers.get("Authorization")

    patches = _install_chat_mocks(["回答1。"])
    for p in patches:
        p.start()
    try:
        await _ask_once(user_client, "用户A的问题")

        await create_user_in_db("clear_b", "clearb@test.com", "123456")
        resp = await client.post("/api/v1/auth/login", json={
            "username": "clear_b", "password": "123456"
        })
        client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

        resp = await client.post("/api/v1/conversations/clear")
        assert resp.json()["deleted"] == 0  # 用户B自己没有会话

        # 恢复用户A的 token，验证其会话不受影响
        client.headers["Authorization"] = auth_a
        resp = await client.get("/api/v1/conversations")
        assert resp.json()["total"] == 1
    finally:
        for p in patches:
            p.stop()
        if auth_a is not None:
            client.headers["Authorization"] = auth_a
        else:
            client.headers.pop("Authorization", None)


@pytest.mark.asyncio
async def test_clear_requires_auth(client: AsyncClient):
    """未认证 401"""
    resp = await client.post("/api/v1/conversations/clear")
    assert resp.status_code == 401


# ════════════════════════════════════════════════════
# 快捷问题
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_quick_questions_default(user_client: AsyncClient):
    """未配置时返回默认 6 条（fail-open：欢迎页永远有内容；企业知识库口径）"""
    resp = await user_client.get("/api/v1/chat/quick-questions")
    assert resp.status_code == 200
    questions = resp.json()["questions"]
    assert len(questions) == 6
    assert "年假怎么申请？" in questions
    assert "报销流程是什么？" in questions


@pytest.mark.asyncio
async def test_quick_questions_legacy_defaults_upgrade():
    """客服口径旧默认（4 条或 6 条）→ seed 自动升级为企业口径新默认 6 条"""
    import json
    from app.models.setting import SystemSetting
    from app.services import setting_service

    for legacy in (
        setting_service.LEGACY_DEFAULT_QUICK_QUESTIONS_V1,
        setting_service.LEGACY_DEFAULT_QUICK_QUESTIONS_V2,
    ):
        # 每轮先清掉上一轮的行（key 唯一）
        async with async_session_factory() as db:
            row = (await db.execute(
                select(SystemSetting).where(
                    SystemSetting.key == setting_service.QUICK_QUESTIONS_KEY
                )
            )).scalar_one_or_none()
            if row:
                await db.delete(row)
                await db.commit()
            db.add(SystemSetting(
                key=setting_service.QUICK_QUESTIONS_KEY,
                value=json.dumps(legacy, ensure_ascii=False),
            ))
            await db.commit()

        async with async_session_factory() as db:
            await setting_service.seed_default_settings(db)
            row = (await db.execute(
                select(SystemSetting).where(
                    SystemSetting.key == setting_service.QUICK_QUESTIONS_KEY
                )
            )).scalar_one()
            assert row.value == json.dumps(
                setting_service.DEFAULT_QUICK_QUESTIONS, ensure_ascii=False
            )
            await db.rollback()


@pytest.mark.asyncio
async def test_quick_questions_custom_value_not_overwritten():
    """管理员改过的值不会被旧默认升级逻辑覆盖"""
    import json
    from app.models.setting import SystemSetting
    from app.services import setting_service

    custom = json.dumps(["管理员自定义的问题"], ensure_ascii=False)
    async with async_session_factory() as db:
        db.add(SystemSetting(key=setting_service.QUICK_QUESTIONS_KEY, value=custom))
        await db.commit()

    async with async_session_factory() as db:
        await setting_service.seed_default_settings(db)
        row = (await db.execute(
            select(SystemSetting).where(
                SystemSetting.key == setting_service.QUICK_QUESTIONS_KEY
            )
        )).scalar_one()
        assert row.value == custom
        await db.rollback()


@pytest.mark.asyncio
async def test_quick_questions_admin_update_then_read(admin_client: AsyncClient):
    """管理端更新后用户端读到新配置"""
    resp = await admin_client.put(
        "/api/v1/admin/settings/quick-questions",
        json={"questions": ["这件衣服有XL码吗？", "怎么退货？"]},
    )
    assert resp.status_code == 200

    resp = await admin_client.get("/api/v1/chat/quick-questions")
    assert resp.json()["questions"] == ["这件衣服有XL码吗？", "怎么退货？"]


@pytest.mark.asyncio
async def test_quick_questions_update_validation(admin_client: AsyncClient):
    """条数与长度校验"""
    # 空列表：Pydantic min_length 拦下
    resp = await admin_client.put(
        "/api/v1/admin/settings/quick-questions", json={"questions": []}
    )
    assert resp.status_code == 422

    # 超过 8 条 → 400
    resp = await admin_client.put(
        "/api/v1/admin/settings/quick-questions",
        json={"questions": [f"问题{i}" for i in range(9)]},
    )
    assert resp.status_code == 400
    assert "1~8" in resp.json()["detail"]

    # 单条超 30 字 → 400
    resp = await admin_client.put(
        "/api/v1/admin/settings/quick-questions",
        json={"questions": ["这" * 31]},
    )
    assert resp.status_code == 400
    assert "30 字" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_quick_questions_update_requires_admin(user_client: AsyncClient):
    """普通用户改配置 → 403"""
    resp = await user_client.put(
        "/api/v1/admin/settings/quick-questions",
        json={"questions": ["一条问题"]},
    )
    assert resp.status_code == 403
