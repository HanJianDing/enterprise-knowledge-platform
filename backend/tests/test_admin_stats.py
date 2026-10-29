"""图表看板统计接口测试 — GET /admin/dashboard/stats（直接落库造数据，不调用真实 LLM）"""
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.infrastructure.database import async_session_factory
from app.models.conversation import Conversation
from app.models.document import Document, KnowledgeCategory
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


async def _seed_activity(user_id: str) -> None:
    """造趋势/质量数据：
    - 会话：今天 1、2 天前 1、10 天前 1（10 天前应落在 days=7 窗口外）
    - 消息：今天 assistant×2（latency 100/300）+ 1 条 user 消息；2 天前 assistant×1（latency 800）
    - 评价：2 赞 1 踩
    """
    now = datetime.now()
    async with async_session_factory() as db:
        convs = [
            Conversation(user_id=user_id, title="t1", status="active",
                         created_at=now),
            Conversation(user_id=user_id, title="t2", status="active",
                         created_at=now - timedelta(days=2)),
            Conversation(user_id=user_id, title="t3", status="active",
                         created_at=now - timedelta(days=10)),
        ]
        db.add_all(convs)
        await db.flush()
        c1, c2, c3 = convs[0], convs[1], convs[2]

        msgs = [
            Message(conversation_id=c1.id, role="assistant", content="a1",
                    latency_ms=100, created_at=now),
            Message(conversation_id=c1.id, role="assistant", content="a2",
                    latency_ms=300, created_at=now),
            Message(conversation_id=c1.id, role="user", content="q",
                    latency_ms=0, created_at=now),
            Message(conversation_id=c2.id, role="assistant", content="a3",
                    latency_ms=800, created_at=now - timedelta(days=2)),
        ]
        db.add_all(msgs)
        await db.flush()

        for i, m in enumerate(msgs[:3]):
            db.add(MessageFeedback(
                message_id=m.id, user_id=user_id,
                rating="like" if i < 2 else "dislike",
            ))
        await db.commit()


async def _seed_categories(user_id: str, kb_id: str) -> None:
    """分类文档分布：分类A×2、分类B×1、未分类×1（企业知识库阶段1：分类/文档归属知识库）"""
    async with async_session_factory() as db:
        cat_a = KnowledgeCategory(name="统计分类A", kb_id=kb_id)
        cat_b = KnowledgeCategory(name="统计分类B", kb_id=kb_id)
        db.add_all([cat_a, cat_b])
        await db.flush()

        def _doc(name: str, cat_id):
            return Document(
                filename=name, original_name=name, file_type="txt",
                file_size=10, storage_path=f"/tmp/{name}", kb_id=kb_id, category_id=cat_id,
                content_type="long_text", status="completed", chunk_count=1,
                uploaded_by=user_id,
            )

        db.add_all([
            _doc("a1.txt", cat_a.id), _doc("a2.txt", cat_a.id),
            _doc("b1.txt", cat_b.id),
            _doc("n1.txt", None),
        ])
        await db.commit()


@pytest.mark.asyncio
async def test_stats_trend_zero_filled(admin_client: AsyncClient):
    """趋势：days=7 窗口内按天计数、缺失日期补零、窗口外数据不计"""
    user_id = await _seed_user("stats_u1", "stats1@test.com")
    await _seed_activity(user_id)

    resp = await admin_client.get("/api/v1/admin/dashboard/stats", params={"days": 7})
    assert resp.status_code == 200
    trend = resp.json()["trend"]
    assert len(trend) == 7

    today = datetime.now().date().isoformat()
    two_days_ago = (datetime.now() - timedelta(days=2)).date().isoformat()
    by_day = {row["date"]: row for row in trend}

    assert by_day[today]["conversations"] == 1
    assert by_day[today]["messages"] == 3
    assert by_day[two_days_ago]["conversations"] == 1
    assert by_day[two_days_ago]["messages"] == 1
    # 10 天前的会话在 7 天窗口外
    assert sum(row["conversations"] for row in trend) == 2
    # 补零：存在全 0 的日期
    assert any(row["conversations"] == 0 and row["messages"] == 0 for row in trend)


@pytest.mark.asyncio
async def test_stats_quality_average(admin_client: AsyncClient):
    """质量口径：assistant 且 latency>0 的均值（user 消息与 latency=0 不计）"""
    user_id = await _seed_user("stats_u2", "stats2@test.com")
    await _seed_activity(user_id)

    resp = await admin_client.get("/api/v1/admin/dashboard/stats")
    quality = resp.json()["quality"]
    # (100 + 300 + 800) / 3 = 400
    assert quality["avg_latency_ms"] == 400


@pytest.mark.asyncio
async def test_stats_feedback_embedded(admin_client: AsyncClient):
    """看板数据内嵌满意度（复用阶段3评价数据）"""
    user_id = await _seed_user("stats_u3", "stats3@test.com")
    await _seed_activity(user_id)

    resp = await admin_client.get("/api/v1/admin/dashboard/stats")
    fb = resp.json()["feedback"]
    assert fb["like"] == 2
    assert fb["dislike"] == 1
    assert fb["satisfaction_rate"] == 66.7


@pytest.mark.asyncio
async def test_stats_category_distribution(admin_client: AsyncClient, default_kb: str):
    """分类分布：数量倒序、未分类单列、名称正确（平铺分类=自身即主题）"""
    user_id = await _seed_user("stats_u4", "stats4@test.com")
    await _seed_categories(user_id, default_kb)

    resp = await admin_client.get("/api/v1/admin/dashboard/stats")
    cats = resp.json()["categories"]
    assert cats[0]["name"] == "统计分类A"
    assert cats[0]["count"] == 2
    assert cats[1]["name"] == "统计分类B"
    assert cats[1]["count"] == 1
    assert cats[2]["name"] == "未分类"
    assert cats[2]["count"] == 1


@pytest.mark.asyncio
async def test_stats_category_distribution_by_theme(admin_client: AsyncClient, default_kb: str):
    """批次I：两层分类树下按主题父分类归并——同名形态子分类不再重复出现，
    每个主题一条、数字为该主题下所有形态的文档总数"""
    user_id = await _seed_user("stats_u5", "stats5@test.com")
    async with async_session_factory() as db:
        theme = KnowledgeCategory(name="考勤", kb_id=default_kb)
        other = KnowledgeCategory(name="假期", kb_id=default_kb)
        db.add_all([theme, other])
        await db.flush()
        sub1 = KnowledgeCategory(name="制度", kb_id=default_kb, parent_id=theme.id)
        sub2 = KnowledgeCategory(name="流程", kb_id=default_kb, parent_id=theme.id)
        sub3 = KnowledgeCategory(name="制度", kb_id=default_kb, parent_id=other.id)
        db.add_all([sub1, sub2, sub3])
        await db.flush()

        def _doc(name: str, cat_id):
            return Document(
                filename=name, original_name=name, file_type="txt",
                file_size=10, storage_path=f"/tmp/{name}", kb_id=default_kb,
                category_id=cat_id, content_type="long_text",
                status="completed", chunk_count=1, uploaded_by=user_id,
            )

        db.add_all([
            _doc("kaoqin1.txt", sub1.id), _doc("kaoqin2.txt", sub2.id),
            _doc("jiaqi1.txt", sub3.id),
        ])
        await db.commit()

    resp = await admin_client.get("/api/v1/admin/dashboard/stats")
    cats = {c["name"]: c["count"] for c in resp.json()["categories"]}
    # 子分类归并到各自主题；"制度"这种形态名不再作为横条出现
    assert cats["考勤"] == 2
    assert cats["假期"] == 1
    assert "制度" not in cats
    assert "流程" not in cats


@pytest.mark.asyncio
async def test_stats_empty_db_all_zero(admin_client: AsyncClient):
    """空库：趋势全 0、满意度 rate=null、质量 null、分类空"""
    resp = await admin_client.get("/api/v1/admin/dashboard/stats", params={"days": 3})
    data = resp.json()
    assert len(data["trend"]) == 3
    assert all(r["conversations"] == 0 for r in data["trend"])
    assert data["feedback"]["satisfaction_rate"] is None
    assert data["quality"]["avg_latency_ms"] is None
    assert data["categories"] == []


@pytest.mark.asyncio
async def test_stats_days_validation(admin_client: AsyncClient):
    """days 参数校验：<1 或 >90 → 422"""
    assert (await admin_client.get(
        "/api/v1/admin/dashboard/stats", params={"days": 0}
    )).status_code == 422
    assert (await admin_client.get(
        "/api/v1/admin/dashboard/stats", params={"days": 91}
    )).status_code == 422


@pytest.mark.asyncio
async def test_stats_requires_admin(user_client: AsyncClient):
    """普通用户 → 403"""
    resp = await user_client.get("/api/v1/admin/dashboard/stats")
    assert resp.status_code == 403
