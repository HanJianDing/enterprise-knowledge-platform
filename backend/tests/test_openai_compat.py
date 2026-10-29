"""批次11e OpenAI 兼容 API 测试：消息提取 / key 哈希 / 协议格式"""
import pytest

from app.api.v1.openai_compat import (
    ChatMessage,
    _extract_question,
    _hash_key,
    _completion_payload,
)


class TestExtractQuestion:
    def test_last_user_message_wins(self):
        msgs = [
            ChatMessage(role="user", content="第一个问题"),
            ChatMessage(role="assistant", content="第一个回答"),
            ChatMessage(role="user", content="第二个问题"),
        ]
        question, history = _extract_question(msgs)
        assert question == "第二个问题"
        # 批次A·11e：多轮历史注入——最后一条 user 之外的全部消息转内部格式
        assert history == [
            {"role": "user", "content": "第一个问题"},
            {"role": "assistant", "content": "第一个回答"},
        ]

    def test_no_user_message_returns_empty(self):
        msgs = [ChatMessage(role="system", content="你是助手")]
        question, history = _extract_question(msgs)
        assert question == ""
        assert history == []

    def test_system_then_user(self):
        msgs = [
            ChatMessage(role="system", content="设定"),
            ChatMessage(role="user", content="  实际问题  "),
        ]
        question, history = _extract_question(msgs)
        assert question == "实际问题"
        assert history == []  # system 消息被忽略

    def test_single_user_no_history(self):
        msgs = [ChatMessage(role="user", content="就一个问题")]
        question, history = _extract_question(msgs)
        assert question == "就一个问题"
        assert history == []


class TestKeyHash:
    def test_hash_stable_and_hex(self):
        h = _hash_key("sk-rag-test")
        assert len(h) == 64
        assert _hash_key("sk-rag-test") == h
        assert _hash_key("sk-rag-other") != h


class TestPayload:
    def test_openai_shape(self):
        payload = _completion_payload("答案内容", {"prompt_tokens": 0, "completion_tokens": 5, "total_tokens": 5})
        assert payload["object"] == "chat.completion"
        assert payload["choices"][0]["message"]["role"] == "assistant"
        assert payload["choices"][0]["message"]["content"] == "答案内容"
        assert payload["choices"][0]["finish_reason"] == "stop"
        assert payload["usage"]["total_tokens"] == 5


class TestInputLimits:
    """批次A 质检 P2 回归钉住：OpenAI 门输入限长（与员工端同口径）"""

    @pytest.mark.asyncio
    async def test_overlong_content_rejected_422(self, client, admin_token, monkeypatch):
        import app.services.audit_service as audit_svc

        async def fake_audit(*a, **k):
            return None

        monkeypatch.setattr(audit_svc, "record_audit", fake_audit)
        client.headers["Authorization"] = f"Bearer {admin_token}"
        # 构造合法 key 后投递超长消息
        from sqlalchemy import select
        from app.infrastructure.database import async_session_factory
        from app.models.user import User

        async with async_session_factory() as db:
            owner = (await db.execute(
                select(User.id).where(User.role == "admin").limit(1)
            )).scalar_one()
            import hashlib
            from app.models.api_key import ApiKey

            plain = "sk-rag-limit-test"
            db.add(ApiKey(
                key_hash=hashlib.sha256(plain.encode()).hexdigest(),
                key_prefix=plain[:16], name="限长测试", user_id=owner,
            ))
            await db.commit()

        resp = await client.post("/v1/chat/completions", json={
            "model": "rag-knowledge-base",
            "messages": [{"role": "user", "content": "长" * 2001}],
        }, headers={"Authorization": f"Bearer {plain}"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_too_many_messages_rejected_422(self, client):
        resp = await client.post("/v1/chat/completions", json={
            "model": "rag-knowledge-base",
            "messages": [
                {"role": "user", "content": f"消息{i}"} for i in range(21)
            ],
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_boundary_values_pass_validation(self, client):
        """批次B P3 钉住边界通过值：恰 2000 字 / 恰 20 条不触发 422
        （422 只应出现在越界时；无 key 场景走鉴权 401 而非校验 422）"""
        resp = await client.post("/v1/chat/completions", json={
            "model": "rag-knowledge-base",
            "messages": [
                {"role": "user", "content": "字" * 2000}
                for _ in range(20)
            ],
        })
        # 校验层通过 → 落到鉴权 401（本测试无 key），而非 422
        assert resp.status_code == 401


class TestApiKeyExpiry:
    """批次A·11e backlog：expires_at 管理入口（创建可设/列表展示/修改端点）"""

    @pytest.mark.asyncio
    async def test_create_with_expiry(self, client, admin_token, monkeypatch):
        from app.config import settings as cfg
        import app.services.audit_service as audit_svc

        async def fake_audit(*a, **k):
            return None

        monkeypatch.setattr(audit_svc, "record_audit", fake_audit)
        client.headers["Authorization"] = f"Bearer {admin_token}"
        resp = await client.post("/api/v1/admin/api-keys", json={
            "name": "带过期的key",
            "expires_at": "2027-01-01T00:00:00Z",
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["expires_at"] is not None
        assert "2027-01-01" in body["expires_at"]

        # 列表含 expires_at 字段
        lst = await client.get("/api/v1/admin/api-keys")
        assert lst.status_code == 200
        item = next(k for k in lst.json() if k["id"] == body["id"])
        assert item["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_create_invalid_expiry_400(self, client, admin_token):
        client.headers["Authorization"] = f"Bearer {admin_token}"
        resp = await client.post("/api/v1/admin/api-keys", json={
            "name": "格式错",
            "expires_at": "不是日期",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_set_and_clear_expiry(self, client, admin_token, monkeypatch):
        import app.services.audit_service as audit_svc

        async def fake_audit(*a, **k):
            return None

        monkeypatch.setattr(audit_svc, "record_audit", fake_audit)
        client.headers["Authorization"] = f"Bearer {admin_token}"
        resp = await client.post("/api/v1/admin/api-keys", json={"name": "改过期"})
        key_id = resp.json()["id"]

        # 设置过期
        r1 = await client.patch(f"/api/v1/admin/api-keys/{key_id}/expiry",
                                json={"expires_at": "2027-06-30T12:00:00Z"})
        assert r1.status_code == 200
        assert "2027-06-30" in r1.json()["expires_at"]

        # 清除过期（永不过期）
        r2 = await client.patch(f"/api/v1/admin/api-keys/{key_id}/expiry",
                                json={"expires_at": None})
        assert r2.status_code == 200
        assert r2.json()["expires_at"] is None

    @pytest.mark.asyncio
    async def test_expired_key_rejected_401(self, client, admin_token, monkeypatch):
        """过期 key 鉴权拒 401（鉴权侧已按 expires_at 校验——11e 已实现，
        此处端到端钉住过期路径）"""
        import app.services.audit_service as audit_svc

        async def fake_audit(*a, **k):
            return None

        monkeypatch.setattr(audit_svc, "record_audit", fake_audit)
        client.headers["Authorization"] = f"Bearer {admin_token}"
        # 用管理 API 建一个已过期的 key（直接落库构造，绕创建端点校验）
        import hashlib
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select
        from app.infrastructure.database import async_session_factory
        from app.models.api_key import ApiKey
        from app.models.user import User

        plain = "sk-rag-expired-test"
        async with async_session_factory() as db:
            owner = (await db.execute(
                select(User.id).where(User.role == "admin").limit(1)
            )).scalar_one()
            db.add(ApiKey(
                key_hash=hashlib.sha256(plain.encode()).hexdigest(),
                key_prefix=plain[:16], name="已过期测试",
                user_id=owner,
                expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            ))
            await db.commit()

        resp = await client.post("/v1/chat/completions", json={
            "model": "rag-knowledge-base",
            "messages": [{"role": "user", "content": "测试"}],
        }, headers={"Authorization": f"Bearer {plain}"})
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"]


class TestApiKeyAuth:
    @pytest.mark.asyncio
    async def test_missing_key_rejected(self, client):
        resp = await client.post("/v1/chat/completions", json={
            "model": "rag-knowledge-base",
            "messages": [{"role": "user", "content": "测试"}],
        })
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_key_rejected(self, client):
        resp = await client.post("/v1/chat/completions", json={
            "model": "rag-knowledge-base",
            "messages": [{"role": "user", "content": "测试"}],
        }, headers={"Authorization": "Bearer sk-rag-invalid"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_rate_limit_returns_429(self, client, monkeypatch):
        """P1-3 回归钉住：key 维度限频超限 → 429（在 DB 校验之前拦截）"""
        from app.api.v1 import openai_compat

        async def fake_auth(request, db):
            # 绕过鉴权（限频在鉴权内，此处直接测限频路径需 mock limiter 拒绝）
            raise NotImplementedError

        # 用 mock 限频器拒绝模拟超限
        class FakeLimiter:
            def allow(self, key, limit):
                return False

        # 限频器是函数内 from app.middleware.rate_limit import——patch 源模块属性
        monkeypatch.setattr("app.middleware.rate_limit._limiter", FakeLimiter())
        monkeypatch.setattr("app.config.settings.rate_limit_enabled", True)
        resp = await client.post("/v1/chat/completions", json={
            "model": "rag-knowledge-base",
            "messages": [{"role": "user", "content": "测试"}],
        }, headers={"Authorization": "Bearer sk-rag-anykey"})
        assert resp.status_code == 429
