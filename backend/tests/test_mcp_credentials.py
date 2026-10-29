"""批次D 测试：外部接入凭证（管理 CRUD + MCP 验证通道 + 库白名单限制）"""
import hashlib

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.infrastructure.database import async_session_factory
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.mcp_server import JwtTokenVerifier, _resolve_user
from app.services import mcp_service
from tests.conftest import create_user_in_db
from tests.test_mcp_server import _fake_ctx


class TestCredentialApi:
    @pytest.mark.asyncio
    async def test_create_and_list(self, admin_client: AsyncClient):
        """新建凭证：一次性返回 mcpk_ 原文；列表不含原文"""
        resp = await admin_client.post("/api/v1/admin/mcp-credentials", json={
            "name": "OA系统",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["token"].startswith("mcpk_")
        assert "仅本次展示" in data["message"]

        resp = await admin_client.get("/api/v1/admin/mcp-credentials")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "OA系统"
        assert "token" not in items[0]  # 列表绝不返回原文

    @pytest.mark.asyncio
    async def test_create_invalid_kb_rejected(self, admin_client: AsyncClient):
        resp = await admin_client.post("/api/v1/admin/mcp-credentials", json={
            "name": "坏凭证", "kb_ids": ["not-exist-kb"],
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_revoke_flow(self, admin_client: AsyncClient):
        """撤销后：MCP 校验拒绝；重复撤销 400"""
        resp = await admin_client.post("/api/v1/admin/mcp-credentials", json={"name": "临时接入"})
        cred_id = resp.json()["id"]
        token = resp.json()["token"]

        # 撤销前可验证
        access = await JwtTokenVerifier().verify_token(token)
        assert access is not None
        assert access.claims.get("credential") is True

        resp = await admin_client.post(f"/api/v1/admin/mcp-credentials/{cred_id}/revoke")
        assert resp.status_code == 200
        resp = await admin_client.post(f"/api/v1/admin/mcp-credentials/{cred_id}/revoke")
        assert resp.status_code == 400

        # 撤销后验证拒绝
        assert await JwtTokenVerifier().verify_token(token) is None

    @pytest.mark.asyncio
    async def test_verifier_garbage_token_rejected(self):
        assert await JwtTokenVerifier().verify_token("mcpk_deadbeef") is None
        assert await JwtTokenVerifier().verify_token("garbage") is None

    @pytest.mark.asyncio
    async def test_resolve_credential_skips_sv_check(self):
        """接入凭证模式跳过 sv 顶号校验（独立生命周期，撤销即失效——设计取舍）"""
        await create_user_in_db("cred_owner", "cred_owner@test.com")
        async with async_session_factory() as db:
            owner = (await db.execute(
                select(User).where(User.username == "cred_owner")
            )).scalar_one()
            # 故意给错误的 sv + credential 标记：凭证模式不校验 sv，正常解析
            ctx = _fake_ctx(True, {"user_id": owner.id, "role": "user",
                                   "sv": 999, "credential": True, "kb_restrict": None})
            resolved, restrict = await _resolve_user(ctx)
        assert resolved is not None
        assert resolved.username == "cred_owner"
        assert restrict is None


class TestKbRestriction:
    @pytest.mark.asyncio
    async def test_search_respects_kb_restrict(self, default_kb: str, monkeypatch):
        """接入凭证库白名单：检索范围 = 账号可见 ∩ 白名单（批量过滤实现）"""
        from app.models.document import Document as Doc

        await create_user_in_db("cred_user", "cred_user@test.com")
        async with async_session_factory() as db:
            user = (await db.execute(
                select(User).where(User.username == "cred_user")
            )).scalar_one()
            # 第二个库 + 两个库各一条文档
            other_kb = KnowledgeBase(name="其他库", is_public=True, created_by=user.id)
            db.add(other_kb)
            await db.flush()
            for kb_id, name in ((default_kb, "人事文档.txt"), (other_kb.id, "其他文档.txt")):
                db.add(Doc(
                    filename=name, original_name=name, file_type="txt",
                    file_size=10, storage_path=name, kb_id=kb_id,
                    content_type="long_text", status="completed", chunk_count=1,
                    uploaded_by=user.id,
                ))
            await db.commit()
            other_kb_id = other_kb.id

        captured: dict = {}

        def fake_retrieve(query, top_k=6, doc_ids=None, **kw):
            captured["doc_ids"] = list(doc_ids or [])
            return []

        monkeypatch.setattr(mcp_service, "retrieve_only", fake_retrieve)

        # 白名单只允许 default_kb → 检索范围只剩 default_kb 的文档
        await mcp_service.search_for_user(user, "测试", 3, kb_restrict=[default_kb])
        assert len(captured["doc_ids"]) == 1

        # 白名单允许两个库 → 两个库文档都进检索范围
        await mcp_service.search_for_user(user, "测试", 3, kb_restrict=[default_kb, other_kb_id])
        assert len(captured["doc_ids"]) == 2