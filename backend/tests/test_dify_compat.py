"""批次E·6b Dify 兼容检索 API 测试：鉴权 / 协议结构 / 越权库拒绝"""
import pytest


class TestDifyRetrieval:
    @pytest.mark.asyncio
    async def test_missing_key_rejected(self, client):
        resp = await client.post("/v1/dify/retrieval", json={
            "query": "测试问题",
        })
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_key_rejected(self, client):
        resp = await client.post("/v1/dify/retrieval", json={
            "query": "测试问题",
        }, headers={"Authorization": "Bearer sk-invalid"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_visible_range_returns_empty_records(
        self, client, admin_token, monkeypatch
    ):
        """无可见文档 → 空 records（不 500）"""
        import hashlib

        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.api_key import ApiKey
        from app.models.user import User
        from app.services import kb_service

        # 构造合法 key
        plain = "sk-rag-dify-test"
        async with async_session_factory() as db:
            owner = (await db.execute(
                select(User.id).where(User.role == "admin").limit(1)
            )).scalar_one()
            db.add(ApiKey(
                key_hash=hashlib.sha256(plain.encode()).hexdigest(),
                key_prefix=plain[:16], name="dify测试", user_id=owner,
            ))
            await db.commit()

        # mock 可见文档为空（不触真实检索/嵌入）
        async def fake_visible(db, user):
            return []

        monkeypatch.setattr(kb_service, "get_visible_doc_ids", fake_visible)

        resp = await client.post("/v1/dify/retrieval", json={
            "query": "测试问题",
            "retrieval_setting": {"top_k": 3, "score_threshold": 0.5},
        }, headers={"Authorization": f"Bearer {plain}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["records"] == []

    @pytest.mark.asyncio
    async def test_unknown_kb_rejected_403(self, client, admin_token, monkeypatch):
        """knowledge_id 指定不在可见范围的库 → 403"""
        import hashlib

        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.api_key import ApiKey
        from app.models.user import User
        from app.services import kb_service

        plain = "sk-rag-dify-403"
        async with async_session_factory() as db:
            owner = (await db.execute(
                select(User.id).where(User.role == "admin").limit(1)
            )).scalar_one()
            db.add(ApiKey(
                key_hash=hashlib.sha256(plain.encode()).hexdigest(),
                key_prefix=plain[:16], name="dify403", user_id=owner,
            ))
            await db.commit()

        async def fake_docs(db, user):
            return ["doc-1"]

        async def fake_kbs(db, user):
            return ["kb-visible"]

        monkeypatch.setattr(kb_service, "get_visible_doc_ids", fake_docs)
        monkeypatch.setattr(kb_service, "get_visible_kb_ids", fake_kbs)

        resp = await client.post("/v1/dify/retrieval", json={
            "knowledge_id": "kb:kb-other",
            "query": "测试",
        }, headers={"Authorization": f"Bearer {plain}"})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_request_validation(self, client):
        """top_k 越界 → 422（Pydantic 校验先于函数体鉴权——协议约束独立）"""
        resp = await client.post("/v1/dify/retrieval", json={
            "query": "x",
            "retrieval_setting": {"top_k": 100, "score_threshold": 0.5},
        })
        assert resp.status_code == 422
        resp2 = await client.post("/v1/dify/retrieval", json={
            "query": "",
        })
        assert resp2.status_code == 422  # query 空串同样校验拒绝
