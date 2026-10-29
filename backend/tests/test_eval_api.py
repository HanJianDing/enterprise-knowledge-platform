"""产品化 B4 测试：检索评估服务与 API（试检索 / 评估跑分）"""
import json

import pytest
from httpx import AsyncClient
from langchain_core.documents import Document

from app.services import eval_service
from tests.conftest import create_user_in_db


async def _ret(value):
    """async 恒等包装：给 monkeypatch 的 load_all_doc_ids 用（async 函数需返回协程）"""
    return value


def _fake_docs(filenames: list[str]) -> list[Document]:
    return [
        Document(
            page_content=f"{name} 的内容片段",
            metadata={"document_id": f"doc-{i}", "chunk_index": i,
                      "filename": name},
        )
        for i, name in enumerate(filenames)
    ]


# ════════════════════════════════════════════════════
# eval_service.retrieval_test — 试检索
# ════════════════════════════════════════════════════

class TestRetrievalTest:
    def test_formats_hit_docs(self, monkeypatch):
        monkeypatch.setattr(eval_service, "load_all_doc_ids", lambda: _ret(["d1"]))
        monkeypatch.setattr(
            eval_service, "retrieve_only",
            lambda query, top_k=6, doc_ids=None, **kw: _fake_docs(["考勤制度.txt", "年假政策.txt"]),
        )
        result = eval_service.retrieval_test("年假怎么请", 6)
        assert result["query"] == "年假怎么请"
        assert result["reranked"] is False
        assert len(result["docs"]) == 2
        assert result["docs"][0]["index"] == 1
        assert result["docs"][0]["filename"] == "考勤制度.txt"
        assert result["docs"][0]["content_preview"].endswith("的内容片段")

    def test_no_docs_returns_empty(self, monkeypatch):
        monkeypatch.setattr(eval_service, "load_all_doc_ids", lambda: _ret([]))
        result = eval_service.retrieval_test("问题", 6)
        assert result["docs"] == []


# ════════════════════════════════════════════════════
# eval_service.run_eval — 评估跑分
# ════════════════════════════════════════════════════

class TestRunEval:
    def test_metrics_computed_with_mocked_retrieval(self, monkeypatch):
        """每题都命中期望首文档 → hit_rate 1.0 / MRR 1.0；分组数相加等于总题数"""
        queries = eval_service._load_queries("all")  # 验收批次1：评估集分层加载

        def fake_retrieve(query, top_k=6, doc_ids=None, **kw):
            # 按题目查出期望文档名，返回"必定命中"的检索结果
            for item in queries:
                if item["question"] == query:
                    return _fake_docs([item["expected_docs"][0]])
            return _fake_docs(["无关文档.txt"])

        monkeypatch.setattr(eval_service, "load_all_doc_ids", lambda: _ret(["d1"]))
        monkeypatch.setattr(eval_service, "retrieve_only", fake_retrieve)

        result = eval_service.run_eval(with_rerank=False)
        assert result["standard"]["n"] + result["fuzzy"]["n"] == result["total_questions"]
        assert result["total"]["hit_rate"] == 1.0
        assert result["total"]["mrr"] == 1.0
        assert result["total"]["avg_ms"] >= 0
        assert len(result["standard"]["details"]) == result["standard"]["n"]


# ════════════════════════════════════════════════════
# API 端点（admin 专用）
# ════════════════════════════════════════════════════

class TestEvalApi:
    @pytest.mark.asyncio
    async def test_retrieval_test_endpoint(self, admin_client: AsyncClient, monkeypatch):
        """试检索端点：200 且返回命中切片结构（验收修复 A2：async 版在请求循环内执行）"""
        monkeypatch.setattr(
            eval_service, "retrieval_test_async",
            lambda question, top_k=6, with_rerank=False: _ret({
                "query": question, "top_k": top_k, "reranked": False,
                "docs": [{"index": 1, "filename": "考勤制度.txt", "chunk_index": 0,
                          "content": "内容", "content_preview": "内容"}],
            }),
        )
        resp = await admin_client.post("/api/v1/admin/eval/retrieval-test", json={
            "question": "年假怎么请", "top_k": 6,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["docs"][0]["filename"] == "考勤制度.txt"

    @pytest.mark.asyncio
    async def test_run_eval_endpoint_audits(self, admin_client: AsyncClient, monkeypatch):
        """评估跑分端点：200 返回指标结构，并写审计"""
        monkeypatch.setattr(
            eval_service, "run_eval_async",
            lambda with_rerank=False, eval_set="all": _ret({
                "total_questions": 60, "doc_count": 1, "with_rerank": False,
                "elapsed_ms": 100,
                "standard": {"n": 50, "hit_rate": 0.9, "mrr": 0.8, "avg_ms": 180, "details": []},
                "fuzzy": {"n": 10, "hit_rate": 0.8, "mrr": 0.7, "avg_ms": 180, "details": []},
                "total": {"n": 60, "hit_rate": 0.883, "mrr": 0.783, "avg_ms": 180},
            }),
        )
        resp = await admin_client.post("/api/v1/admin/eval/run", json={"with_rerank": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_questions"] == 60
        assert data["total"]["hit_rate"] == 0.883

        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.audit import AuditLog

        async with async_session_factory() as db:
            logs = (await db.execute(
                select(AuditLog).where(AuditLog.action == "eval.run")
            )).scalars().all()
        assert len(logs) == 1

    @pytest.mark.asyncio
    async def test_eval_endpoints_admin_only(self, client: AsyncClient):
        """普通用户调用评估端点 → 403"""
        await create_user_in_db("eval_peeper", "eval_peeper@test.com")
        resp = await client.post("/api/v1/auth/login", json={
            "username": "eval_peeper", "password": "123456"
        })
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.post("/api/v1/admin/eval/retrieval-test",
                                 json={"question": "年假", "top_k": 6}, headers=headers)
        assert resp.status_code == 403
        resp = await client.post("/api/v1/admin/eval/run",
                                 json={"with_rerank": False}, headers=headers)
        assert resp.status_code == 403


class TestGraphApi:
    """批次2-3：图谱可视化与社区刷新端点（admin 专属 + 审计）"""

    @pytest.mark.asyncio
    async def test_graph_overview_endpoint(self, admin_client: AsyncClient, monkeypatch):
        from app.rag.graph import community as community_service

        monkeypatch.setattr(
            community_service, "graph_overview",
            lambda: _ret({"nodes": [{"name": "刘强", "type": "person",
                                     "description": "IT 总监", "community_id": 0}],
                          "edges": [], "communities": []}),
        )
        resp = await admin_client.get("/api/v1/admin/graph/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["nodes"][0]["name"] == "刘强"

    @pytest.mark.asyncio
    async def test_communities_refresh_audits(self, admin_client: AsyncClient, monkeypatch):
        from app.rag.graph import community as community_service

        monkeypatch.setattr(
            community_service, "run_community_detection",
            lambda: _ret({"partition": {"刘强": 0}, "community_count": 1}),
        )
        resp = await admin_client.post("/api/v1/admin/graph/communities/refresh")
        assert resp.status_code == 200
        assert resp.json()["community_count"] == 1

        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.audit import AuditLog

        async with async_session_factory() as db:
            logs = (await db.execute(
                select(AuditLog).where(AuditLog.action == "graph.communities_refresh")
            )).scalars().all()
        assert len(logs) == 1

    @pytest.mark.asyncio
    async def test_graph_endpoints_admin_only(self, client: AsyncClient):
        """普通用户调用图谱端点 → 403"""
        await create_user_in_db("graph_peeper", "graph_peeper@test.com")
        resp = await client.post("/api/v1/auth/login", json={
            "username": "graph_peeper", "password": "123456"
        })
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.get("/api/v1/admin/graph/overview", headers=headers)
        assert resp.status_code == 403
        resp = await client.post("/api/v1/admin/graph/communities/refresh", headers=headers)
        assert resp.status_code == 403


class TestApprovalRulesApi:
    """批次2-5：审批规则列表端点（admin 专属）"""

    @pytest.mark.asyncio
    async def test_approval_rules_list(self, admin_client: AsyncClient, monkeypatch):
        """规则列表端点：200 返回规则条目（含来源文档名）"""
        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.rule import ApprovalRule

        # 直插一条规则供端点读取
        import uuid as uuid_mod

        async with async_session_factory() as db:
            db.add(ApprovalRule(
                id=str(uuid_mod.uuid4()), business_type="报销",
                min_value=1000.0, max_value=5000.0, unit="元",
                approver="部门负责人", action="审批",
                source_doc_id="no-such-doc",
                source_text="报销1000 至 5000元：部门负责人审批",
            ))
            await db.commit()

        resp = await admin_client.get("/api/v1/admin/approval-rules")
        assert resp.status_code == 200
        rules = resp.json()
        assert isinstance(rules, list) and len(rules) >= 1
        hit = next(r for r in rules if r["approver"] == "部门负责人")
        assert hit["business_type"] == "报销"
        assert hit["source_doc"] == "已删除文档"

        # 清理
        from sqlalchemy import delete

        async with async_session_factory() as db:
            await db.execute(delete(ApprovalRule).where(
                ApprovalRule.source_doc_id == "no-such-doc"
            ))
            await db.commit()

    @pytest.mark.asyncio
    async def test_approval_rules_admin_only(self, client: AsyncClient):
        """普通用户调用规则端点 → 403"""
        await create_user_in_db("rules_peeper", "rules_peeper@test.com")
        resp = await client.post("/api/v1/auth/login", json={
            "username": "rules_peeper", "password": "123456"
        })
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get("/api/v1/admin/approval-rules", headers=headers)
        assert resp.status_code == 403
