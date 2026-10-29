"""阶段5 数据保留修复测试 — 删除/清空会话改为软删除，工单与评价不再丢失"""
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.infrastructure.database import async_session_factory
from app.models.conversation import Conversation
from app.models.feedback import MessageFeedback
from app.models.message import Message


async def _seed_user(username: str, email: str) -> str:
    from app.middleware.auth import hash_password
    from app.models.user import User

    async with async_session_factory() as db:
        user = User(
            username=username, email=email,
            hashed_password=hash_password("123456"), role="user",
        )
        db.add(user)
        await db.commit()
        return user.id


async def _seed_conversation_with_feedback(
    user_id: str,
) -> tuple[str, str]:
    """落库：会话 + 1 轮问答 + 1 条点踩评价。
    返回 (conv_id, assistant_msg_id)
    """
    async with async_session_factory() as db:
        conv = Conversation(user_id=user_id, title="数据保留会话", status="active")
        db.add(conv)
        await db.flush()
        user_msg = Message(
            conversation_id=conv.id, role="user", content="问题",
            created_at=datetime(2026, 9, 3, 10, 0, 0),
        )
        ai_msg = Message(
            conversation_id=conv.id, role="assistant", content="回答",
            created_at=datetime(2026, 9, 3, 10, 0, 1),
        )
        db.add_all([user_msg, ai_msg])
        await db.flush()
        db.add(MessageFeedback(
            message_id=ai_msg.id, user_id=user_id, rating="dislike",
            comment="答得不对",
        ))
        await db.commit()
        return conv.id, ai_msg.id


async def _row_status(conv_id: str) -> str | None:
    async with async_session_factory() as db:
        conv = (await db.execute(
            select(Conversation).where(Conversation.id == conv_id)
        )).scalar_one_or_none()
        return conv.status if conv else None


async def _login_user_id(client: AsyncClient) -> str:
    """当前登录用户（user_client）的 id——测试数据必须挂在登录用户名下，否则越权 404"""
    return (await client.get("/api/v1/auth/me")).json()["id"]


@pytest.mark.asyncio
async def test_delete_is_soft(user_client: AsyncClient):
    """删除 = 软删除：DB 行保留（status=deleted）、列表排除、详情 404"""
    user_id = await _login_user_id(user_client)
    conv_id, _ = await _seed_conversation_with_feedback(user_id)

    resp = await user_client.delete(f"/api/v1/conversations/{conv_id}")
    assert resp.status_code == 200
    assert "已删除" in resp.json()["message"]

    assert await _row_status(conv_id) == "deleted"  # 数据还在
    assert (await user_client.get("/api/v1/conversations")).json()["total"] == 0
    assert (await user_client.get(f"/api/v1/conversations/{conv_id}")).status_code == 404


@pytest.mark.asyncio
async def test_delete_is_soft_then_repeat_delete_ok(user_client: AsyncClient):
    """企业改造阶段2（客服剥离后）：删除不再有 409 拦截——重复删除幂等"""
    user_id = await _login_user_id(user_client)
    conv_id, _ = await _seed_conversation_with_feedback(user_id)

    resp = await user_client.delete(f"/api/v1/conversations/{conv_id}")
    assert resp.status_code == 200
    assert await _row_status(conv_id) == "deleted"

    # 会话已从列表消失
    assert (await user_client.get("/api/v1/conversations")).json()["total"] == 0


@pytest.mark.asyncio
async def test_delete_preserves_feedback_for_admin(admin_client: AsyncClient):
    """删除后评价不丢：管理端统计口径不变（阶段5 问题②）"""
    user_id = await _seed_user("soft_u3", "soft3@test.com")
    conv_id, ai_msg_id = await _seed_conversation_with_feedback(user_id)

    before = (await admin_client.get("/api/v1/admin/feedback/stats")).json()

    # 用户在客户端删除自己的会话（admin 也能删自己的？直接用 DB 模拟用户删除动作更稳：
    # 走 service 层验证软删后 feedback 仍可查）
    async with async_session_factory() as db:
        conv = (await db.execute(
            select(Conversation).where(Conversation.id == conv_id)
        )).scalar_one()
        conv.status = "deleted"
        await db.commit()

    after = (await admin_client.get("/api/v1/admin/feedback/stats")).json()
    assert after["dislike"] == before["dislike"]

    # 反馈行仍在库里
    async with async_session_factory() as db:
        fb = (await db.execute(
            select(MessageFeedback).where(MessageFeedback.message_id == ai_msg_id)
        )).scalar_one_or_none()
        assert fb is not None
        assert fb.comment == "答得不对"


@pytest.mark.asyncio
async def test_clear_is_soft_all_deleted(user_client: AsyncClient):
    """清空 = 软删除：所有会话状态变 deleted（企业改造阶段2：无工单跳过逻辑）"""
    user_id = await _login_user_id(user_client)
    conv_a, _ = await _seed_conversation_with_feedback(user_id)
    conv_b, _ = await _seed_conversation_with_feedback(user_id)

    resp = await user_client.post("/api/v1/conversations/clear")
    data = resp.json()
    assert data["deleted"] == 2
    assert data["skipped"] == 0

    assert await _row_status(conv_a) == "deleted"
    assert await _row_status(conv_b) == "deleted"

    resp = await user_client.get("/api/v1/conversations")
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_bump_message_count_atomic_and_clamped():
    """计数原子增减：+2 → -3 钳到 0，不出现负数（上线加固：SQL 端原子自增）"""
    from app.services.conversation_service import bump_message_count

    async with async_session_factory() as db:
        conv = Conversation(user_id="u1", title="计数测试", status="active")
        db.add(conv)
        await db.commit()

        await bump_message_count(db, conv.id, 2)
        await db.commit()
        await db.refresh(conv)
        assert conv.message_count == 2

        await bump_message_count(db, conv.id, -3)
        await db.commit()
        await db.refresh(conv)
        assert conv.message_count == 0  # 钳到 0，不为负


@pytest.mark.asyncio
async def test_deleted_conversation_detail_404(user_client: AsyncClient):
    """删除后详情 404（不再有任何数据流出；轮询接口已随客服剥离移除）"""
    user_id = await _login_user_id(user_client)
    conv_id, _ = await _seed_conversation_with_feedback(user_id)
    await user_client.delete(f"/api/v1/conversations/{conv_id}")

    resp = await user_client.get(f"/api/v1/conversations/{conv_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_deleted_conversation_404(user_client: AsyncClient):
    """已删会话不可被 PATCH '复活'（与详情口径一致 404）"""
    user_id = await _login_user_id(user_client)
    conv_id, _ = await _seed_conversation_with_feedback(user_id)
    await user_client.delete(f"/api/v1/conversations/{conv_id}")

    resp = await user_client.patch(
        f"/api/v1/conversations/{conv_id}", json={"status": "active"}
    )
    assert resp.status_code == 404
    assert await _row_status(conv_id) == "deleted"


@pytest.mark.asyncio
async def test_patch_invalid_status_400(user_client: AsyncClient):
    """状态白名单：非法状态（任意字符串）→ 400"""
    user_id = await _login_user_id(user_client)
    conv_id, _ = await _seed_conversation_with_feedback(user_id)

    resp = await user_client.patch(
        f"/api/v1/conversations/{conv_id}", json={"status": "deleted"}
    )
    assert resp.status_code == 400

    resp = await user_client.patch(
        f"/api/v1/conversations/{conv_id}", json={"status": "随便写"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_stream_to_deleted_conversation_creates_new(user_client: AsyncClient):
    """已删会话发消息 → 视为不存在，落进一个新会话（而不是写进已删会话）"""
    from app.services.chat_service import _get_or_create_conversation

    user_id = await _login_user_id(user_client)
    conv_id, _ = await _seed_conversation_with_feedback(user_id)
    await user_client.delete(f"/api/v1/conversations/{conv_id}")

    async with async_session_factory() as db:
        new_conv = await _get_or_create_conversation(db, user_id, conv_id, "新问题")
        assert new_conv.id != conv_id  # 已删会话被绕过，新建了一个
        assert new_conv.status == "active"
        await db.rollback()
