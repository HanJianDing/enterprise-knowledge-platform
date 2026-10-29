"""反馈系统测试 — 点赞/点踩（直接落库造数据，不调用真实 LLM）"""
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.conftest import create_user_in_db
from app.infrastructure.database import async_session_factory
from app.models.conversation import Conversation
from app.models.feedback import MessageFeedback
from app.models.message import Message


async def _seed_conversation(user_id: str) -> tuple[str, str, str]:
    """落库构造会话：1 条 user 消息 + 1 条 assistant 消息。
    返回 (conv_id, user_msg_id, assistant_msg_id)
    """
    async with async_session_factory() as db:
        conv = Conversation(user_id=user_id, title="测试会话", status="active")
        db.add(conv)
        await db.flush()
        user_msg = Message(
            conversation_id=conv.id, role="user", content="测试问题",
            created_at=datetime(2026, 9, 3, 10, 0, 0),
        )
        ai_msg = Message(
            conversation_id=conv.id, role="assistant", content="测试回答",
            created_at=datetime(2026, 9, 3, 10, 0, 1),
        )
        db.add_all([user_msg, ai_msg])
        await db.commit()
        return conv.id, user_msg.id, ai_msg.id


async def _current_user_id(client: AsyncClient) -> str:
    return (await client.get("/api/v1/auth/me")).json()["id"]


async def _seed_user(username: str, email: str) -> str:
    """直接落库造普通用户（避免与 admin_client 共用 client 时 header 互相覆盖）"""
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


# ════════════════════════════════════════════════════
# 提交/修改/取消评价
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_submit_like_and_detail_returns_feedback(user_client: AsyncClient):
    """点赞后：接口返回评价，会话详情该消息带 feedback"""
    user_id = await _current_user_id(user_client)
    conv_id, _, ai_msg_id = await _seed_conversation(user_id)

    resp = await user_client.put(
        f"/api/v1/messages/{ai_msg_id}/feedback",
        json={"rating": "like", "comment": None},
    )
    assert resp.status_code == 200
    assert resp.json()["rating"] == "like"

    detail = (await user_client.get(f"/api/v1/conversations/{conv_id}")).json()
    ai_msg = next(m for m in detail["messages"] if m["id"] == ai_msg_id)
    assert ai_msg["feedback"] == {"rating": "like", "comment": None}
    user_msg = next(m for m in detail["messages"] if m["id"] != ai_msg_id)
    assert user_msg["feedback"] is None


@pytest.mark.asyncio
async def test_like_forces_comment_null(user_client: AsyncClient):
    """语义收紧：点赞不允许带意见——即使传入 comment 也强制置 null"""
    user_id = await _current_user_id(user_client)
    _, _, ai_msg_id = await _seed_conversation(user_id)

    resp = await user_client.put(
        f"/api/v1/messages/{ai_msg_id}/feedback",
        json={"rating": "like", "comment": "好评理由不该被保存"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"rating": "like", "comment": None}

    # 从点踩（带意见）改成点赞 → 意见被清空
    await user_client.put(
        f"/api/v1/messages/{ai_msg_id}/feedback",
        json={"rating": "dislike", "comment": "尺码不对"},
    )
    resp = await user_client.put(
        f"/api/v1/messages/{ai_msg_id}/feedback",
        json={"rating": "like", "comment": None},
    )
    assert resp.json() == {"rating": "like", "comment": None}


@pytest.mark.asyncio
async def test_upsert_changes_rating_and_comment(user_client: AsyncClient):
    """幂等：再点另一侧 = 改评价；comment 同步更新（仍是同一条记录）"""
    user_id = await _current_user_id(user_client)
    _, _, ai_msg_id = await _seed_conversation(user_id)

    await user_client.put(
        f"/api/v1/messages/{ai_msg_id}/feedback",
        json={"rating": "like", "comment": None},
    )
    resp = await user_client.put(
        f"/api/v1/messages/{ai_msg_id}/feedback",
        json={"rating": "dislike", "comment": "尺码不对"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"rating": "dislike", "comment": "尺码不对"}

    # 库中仍只有一条记录（更新而非新增）
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(MessageFeedback).where(MessageFeedback.message_id == ai_msg_id)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].rating == "dislike"
        assert rows[0].comment == "尺码不对"


@pytest.mark.asyncio
async def test_delete_feedback_idempotent(user_client: AsyncClient):
    """取消评价：详情 feedback 回 null；再取消一次幂等成功"""
    user_id = await _current_user_id(user_client)
    conv_id, _, ai_msg_id = await _seed_conversation(user_id)

    await user_client.put(
        f"/api/v1/messages/{ai_msg_id}/feedback",
        json={"rating": "like", "comment": None},
    )
    resp = await user_client.delete(f"/api/v1/messages/{ai_msg_id}/feedback")
    assert resp.status_code == 200

    detail = (await user_client.get(f"/api/v1/conversations/{conv_id}")).json()
    ai_msg = next(m for m in detail["messages"] if m["id"] == ai_msg_id)
    assert ai_msg["feedback"] is None

    # 幂等：没有评价时再取消不报错
    resp = await user_client.delete(f"/api/v1/messages/{ai_msg_id}/feedback")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_feedback_comment_too_long_rejected(user_client: AsyncClient):
    """意见超过 500 字 → 422（Pydantic max_length）"""
    user_id = await _current_user_id(user_client)
    _, _, ai_msg_id = await _seed_conversation(user_id)

    resp = await user_client.put(
        f"/api/v1/messages/{ai_msg_id}/feedback",
        json={"rating": "dislike", "comment": "长" * 501},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_feedback_invalid_rating_rejected(user_client: AsyncClient):
    """非法 rating → 422"""
    user_id = await _current_user_id(user_client)
    _, _, ai_msg_id = await _seed_conversation(user_id)

    resp = await user_client.put(
        f"/api/v1/messages/{ai_msg_id}/feedback",
        json={"rating": "neutral"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_feedback_non_owner_message_404(
    user_client: AsyncClient, client: AsyncClient
):
    """评价别人的消息 → 404（防越权）"""
    user_id = await _current_user_id(user_client)
    _, _, ai_msg_id = await _seed_conversation(user_id)

    await create_user_in_db("fb_other", "fb_other@test.com", "123456")
    resp = await client.post("/api/v1/auth/login", json={
        "username": "fb_other", "password": "123456"
    })
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    try:
        resp = await client.put(
            f"/api/v1/messages/{ai_msg_id}/feedback",
            json={"rating": "like", "comment": None},
        )
        assert resp.status_code == 404
    finally:
        client.headers.pop("Authorization", None)


@pytest.mark.asyncio
async def test_feedback_non_assistant_message_400(user_client: AsyncClient):
    """评价 user 角色消息 → 400（只能评价 AI 回答）"""
    user_id = await _current_user_id(user_client)
    _, user_msg_id, _ = await _seed_conversation(user_id)

    resp = await user_client.put(
        f"/api/v1/messages/{user_msg_id}/feedback",
        json={"rating": "like", "comment": None},
    )
    assert resp.status_code == 400
    assert "AI 回答" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_feedback_requires_auth(client: AsyncClient):
    """未认证 → 401"""
    resp = await client.put(
        "/api/v1/messages/whatever/feedback",
        json={"rating": "like", "comment": None},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unique_constraint_blocks_duplicate(user_client: AsyncClient):
    """数据库层兜底：同一人同一消息插入第二条 → IntegrityError"""
    user_id = await _current_user_id(user_client)
    _, _, ai_msg_id = await _seed_conversation(user_id)

    async with async_session_factory() as db:
        db.add(MessageFeedback(message_id=ai_msg_id, user_id=user_id, rating="like"))
        await db.commit()
        db.add(MessageFeedback(message_id=ai_msg_id, user_id=user_id, rating="dislike"))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


# ════════════════════════════════════════════════════
# 管理端统计与明细
# ════════════════════════════════════════════════════

async def _seed_feedbacks(
    user_id: str,
    likes: int, dislikes: int, dislike_comment: str | None = None,
) -> None:
    """直接落库造多条评价（同一用户对多条 AI 回答的评价，供统计/明细接口用）"""
    async with async_session_factory() as db:
        conv = Conversation(user_id=user_id, title="统计会话", status="active")
        db.add(conv)
        await db.flush()
        for i in range(likes):
            m = Message(
                conversation_id=conv.id, role="assistant",
                content=f"回答{i}", created_at=datetime(2026, 9, 3, 11, 0, i),
            )
            db.add(m)
            await db.flush()
            db.add(MessageFeedback(message_id=m.id, user_id=user_id, rating="like"))
        for i in range(dislikes):
            m = Message(
                conversation_id=conv.id, role="assistant",
                content=f"踩回答{i}", created_at=datetime(2026, 9, 3, 12, 0, i),
            )
            db.add(m)
            await db.flush()
            db.add(MessageFeedback(
                message_id=m.id, user_id=user_id, rating="dislike",
                comment=dislike_comment if i == 0 else None,
            ))
        await db.commit()


@pytest.mark.asyncio
async def test_admin_feedback_stats(admin_client: AsyncClient):
    """统计口径：like/dislike 计数、占比；无数据时 rate 为 null"""
    # 无数据
    resp = await admin_client.get("/api/v1/admin/feedback/stats")
    assert resp.status_code == 200
    assert resp.json()["satisfaction_rate"] is None

    user_id = await _seed_user("fb_stat_user", "fb_stat@test.com")
    _, _, ai_msg_id = await _seed_conversation(user_id)
    await _seed_feedbacks(user_id, likes=2, dislikes=1, dislike_comment="答错了")

    resp = await admin_client.get("/api/v1/admin/feedback/stats")
    data = resp.json()
    assert data["like"] == 2
    assert data["dislike"] == 1
    assert data["total"] == 3
    assert data["satisfaction_rate"] == 66.7


@pytest.mark.asyncio
async def test_admin_feedback_list_filters(admin_client: AsyncClient):
    """明细列表：rating / has_comment 过滤与内容拼装"""
    user_id = await _seed_user("fb_list_user", "fb_list@test.com")
    _, _, ai_msg_id = await _seed_conversation(user_id)
    await _seed_feedbacks(user_id, likes=2, dislikes=1, dislike_comment="答错了")

    # 全部
    resp = await admin_client.get("/api/v1/admin/feedback")
    assert resp.json()["total"] == 3

    # 只看点踩
    resp = await admin_client.get("/api/v1/admin/feedback", params={"rating": "dislike"})
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["comment"] == "答错了"

    # 只看填了意见的
    resp = await admin_client.get("/api/v1/admin/feedback", params={"has_comment": "true"})
    assert resp.json()["total"] == 1

    # 只看没填意见的
    resp = await admin_client.get("/api/v1/admin/feedback", params={"has_comment": "false"})
    assert resp.json()["total"] == 2


@pytest.mark.asyncio
async def test_admin_feedback_requires_admin(user_client: AsyncClient):
    """普通用户访问管理端统计 → 403"""
    resp = await user_client.get("/api/v1/admin/feedback/stats")
    assert resp.status_code == 403
    resp = await user_client.get("/api/v1/admin/feedback")
    assert resp.status_code == 403
