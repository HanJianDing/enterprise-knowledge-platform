"""产品化 B2 测试：MCP Server（token 验证 / 身份解析 fail-closed / 权限检索 / 审计）"""
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from sqlalchemy import select

from app.infrastructure.database import async_session_factory
from app.middleware.auth import create_access_token, create_refresh_token
from app.models.audit import AuditLog
from app.models.user import User
from app.mcp_server import JwtTokenVerifier, _resolve_user
from app.services import mcp_service
from tests.conftest import create_user_in_db


# ════════════════════════════════════════════════════
# JwtTokenVerifier — Bearer token 校验
# ════════════════════════════════════════════════════

class TestJwtTokenVerifier:
    @pytest.mark.asyncio

    async def test_valid_access_token(self):
        verifier = JwtTokenVerifier()
        token = create_access_token("user-1", "user", 0)
        access = await verifier.verify_token(token)
        assert access is not None
        assert access.claims["user_id"] == "user-1"
        assert access.claims["role"] == "user"
        assert access.claims["sv"] == 0  # sv 随 claims 传递供顶号校验

    @pytest.mark.asyncio

    async def test_invalid_token_rejected(self):
        assert await JwtTokenVerifier().verify_token("garbage.token.here") is None

    @pytest.mark.asyncio

    async def test_refresh_token_rejected(self):
        """refresh token 不能用于 MCP 调用（type 校验）"""
        token = create_refresh_token("user-1", 0)
        assert await JwtTokenVerifier().verify_token(token) is None


# ════════════════════════════════════════════════════
# _resolve_user — 身份解析（fail-closed）
# ════════════════════════════════════════════════════

def _fake_ctx(user_obj, claims: dict | None) -> SimpleNamespace:
    """构造与 mcp 请求上下文同构的假对象链：ctx.request_context.request.user"""
    if user_obj is None:
        request = SimpleNamespace(user=None)
    else:
        access = SimpleNamespace(claims=claims)
        request = SimpleNamespace(user=SimpleNamespace(access_token=access))
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


class TestResolveUser:
    @pytest.mark.asyncio

    async def test_no_user_attr_returns_none(self):
        assert await _resolve_user(_fake_ctx(None, None)) == (None, None)

    @pytest.mark.asyncio

    async def test_unknown_user_id_returns_none(self):
        """token 有效但用户不存在/已禁用 → None（fail-closed）"""
        ctx = _fake_ctx(True, {"user_id": "no-such-user", "role": "user"})
        assert await _resolve_user(ctx) == (None, None)

    @pytest.mark.asyncio

    async def test_active_user_resolved(self):
        await create_user_in_db("mcp_real", "mcp_real@test.com")
        async with async_session_factory() as db:
            real = (await db.execute(
                select(User).where(User.username == "mcp_real")
            )).scalar_one()
            # sv 必须与用户当前 session_version 一致（默认 server_default=1）
            ctx = _fake_ctx(True, {"user_id": real.id, "role": "user",
                                   "sv": real.session_version})
            resolved, restrict = await _resolve_user(ctx)
        assert resolved is not None
        assert resolved.username == "mcp_real"
        assert restrict is None

    @pytest.mark.asyncio
    async def test_sv_missing_rejected(self):
        """sv 缺失的 token 拒绝（与业务 API 的 fail-closed 语义一致）"""
        await create_user_in_db("mcp_nosv", "mcp_nosv@test.com")
        async with async_session_factory() as db:
            real = (await db.execute(
                select(User).where(User.username == "mcp_nosv")
            )).scalar_one()
            ctx = _fake_ctx(True, {"user_id": real.id, "role": "user"})
            assert await _resolve_user(ctx) == (None, None)

    @pytest.mark.asyncio
    async def test_sv_mismatch_rejected(self):
        """被其他设备顶号（sv 不一致）的旧 token 拒绝——顶号语义对 MCP 同样生效"""
        await create_user_in_db("mcp_kicked", "mcp_kicked@test.com")
        async with async_session_factory() as db:
            real = (await db.execute(
                select(User).where(User.username == "mcp_kicked")
            )).scalar_one()
            ctx = _fake_ctx(True, {"user_id": real.id, "role": "user", "sv": 99})
            assert await _resolve_user(ctx) == (None, None)


# ════════════════════════════════════════════════════
# mcp_service — 权限检索 + 审计留痕
# ════════════════════════════════════════════════════

class TestMcpService:
    @pytest.mark.asyncio

    async def test_search_returns_cited_context_and_audits(self, default_kb: str, monkeypatch):
        """search_for_user：检索受限可见文档、返回自带引用文本、写审计"""
        from app.models.document import Document as DocModel

        await create_user_in_db("mcp_searcher", "mcp_searcher@test.com")
        async with async_session_factory() as db:
            user = (await db.execute(
                select(User).where(User.username == "mcp_searcher")
            )).scalar_one()
            db.add(DocModel(
                filename="a.txt", original_name="考勤制度.txt", file_type="txt",
                file_size=10, storage_path="a.txt", kb_id=default_kb,
                content_type="long_text", status="completed", chunk_count=1,
                uploaded_by=user.id,
            ))
            await db.commit()

        def fake_retrieve(query, top_k=6, doc_ids=None, **kw):
            # 注意：真实 retrieve_only 是同步阻塞函数（服务里用 asyncio.to_thread 包它），
            # 测试桩也必须是同步的，否则 to_thread 拿到的是协程对象
            return [
                Document(
                    page_content="员工年假为 5 天。",
                    metadata={"document_id": "doc-x", "chunk_index": 0,
                              "filename": "考勤制度.txt"},
                )
            ]

        monkeypatch.setattr(mcp_service, "retrieve_only", fake_retrieve)

        text = await mcp_service.search_for_user(user, "年假几天", 3)
        assert "[来源1] 考勤制度.txt" in text
        assert "员工年假为 5 天" in text
        assert "不要编造" in text  # 返回结构"自带质量"

        async with async_session_factory() as db:
            logs = (await db.execute(
                select(AuditLog).where(AuditLog.action == "mcp.search_kb")
            )).scalars().all()
        assert len(logs) == 1
        assert logs[0].username == "mcp_searcher"
        assert logs[0].detail["question"] == "年假几天"
        assert logs[0].detail["hits"] == 1

    @pytest.mark.asyncio

    async def test_search_empty_question_rejected(self):
        await create_user_in_db("mcp_empty", "mcp_empty@test.com")
        async with async_session_factory() as db:
            user = (await db.execute(
                select(User).where(User.username == "mcp_empty")
            )).scalar_one()
        text = await mcp_service.search_for_user(user, "   ", 3)
        assert "不能为空" in text

    @pytest.mark.asyncio

    async def test_list_kbs_shows_permissions(self, default_kb: str):
        """list_visible_kbs：普通用户看到公共库（查看权限）"""
        from app.models.knowledge_base import KnowledgeBase

        await create_user_in_db("mcp_lister", "mcp_lister@test.com")
        async with async_session_factory() as db:
            user = (await db.execute(
                select(User).where(User.username == "mcp_lister")
            )).scalar_one()
            kb = (await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == default_kb)
            )).scalar_one()

        text = await mcp_service.list_visible_kbs_for_user(user)
        assert kb.name in text
        assert "查看" in text
