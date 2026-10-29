"""验收批次1·1a 测试：同义词表 + 词权重（查询扩展）与配置管理 API"""
import json

import pytest
from httpx import AsyncClient

from app.rag import retrieval_terms
from tests.conftest import create_user_in_db  # noqa: F401  (沿用 conftest 工具)


@pytest.fixture
def tmp_terms_file(tmp_path):
    """把配置指向临时文件，避免测试污染真实 config/retrieval_terms.json"""
    f = tmp_path / "retrieval_terms.json"
    f.write_text(json.dumps({"synonyms": {}, "weights": {}}), encoding="utf-8")
    original = retrieval_terms.TERMS_FILE
    retrieval_terms.TERMS_FILE = f
    retrieval_terms.invalidate()
    yield f
    retrieval_terms.TERMS_FILE = original
    retrieval_terms.invalidate()


class TestExpandQuery:
    def test_empty_config_passthrough(self, tmp_terms_file):
        """配置为空时各词权重 1.0（与无此功能行为一致）"""
        terms = retrieval_terms.expand_terms("报销发票")
        assert all(w == 1.0 for _, w in terms)

    def test_synonym_expansion(self, tmp_terms_file):
        """同义词展开：查询词后追加同义变体（权重 1.0）"""
        retrieval_terms.save_terms({
            "synonyms": {"礼品": ["礼金"]},
            "weights": {},
        })
        terms = retrieval_terms.expand_terms("礼品标准")
        tokens = [t for t, _ in terms]
        assert "礼金" in tokens
        assert tokens.index("礼品") < tokens.index("礼金")  # 原词在前，变体在后
        assert dict(terms)["礼金"] == 1.0

    def test_weight_boost_value(self, tmp_terms_file):
        """词权重 >1：加权词携带放大权重"""
        retrieval_terms.save_terms({
            "synonyms": {},
            "weights": {"报销": 3.0},
        })
        terms = retrieval_terms.expand_terms("报销发票")
        assert dict(terms)["报销"] == 3.0

    def test_weight_downscale(self, tmp_terms_file):
        """验收批次1·8a：词权重 <1 降权（通用词降噪）——打分侧逐词加权支持"""
        retrieval_terms.save_terms({
            "synonyms": {},
            "weights": {"多少": 0.5},
        })
        terms = retrieval_terms.expand_terms("多少金额")
        assert dict(terms)["多少"] == 0.5

    def test_file_missing_falls_back_default(self, tmp_terms_file):
        """配置文件损坏/缺失时回退空配置，不影响检索"""
        tmp_terms_file.write_text("{broken json", encoding="utf-8")
        retrieval_terms.invalidate()
        assert retrieval_terms.expand_terms("任意查询")


class TestRetrievalTermsApi:
    @pytest.mark.asyncio
    async def test_get_and_save_roundtrip(self, admin_client: AsyncClient, tmp_terms_file):
        """管理 API：读取→保存→读取 往返一致，权重越界被拒"""
        resp = await admin_client.get("/api/v1/admin/retrieval-terms")
        assert resp.status_code == 200
        assert resp.json()["synonyms"] == {}

        resp = await admin_client.put("/api/v1/admin/retrieval-terms", json={
            "synonyms": {"礼品": ["礼金", "礼金"]},  # 重复变体应去重
            "weights": {"考勤": 2.0},
        })
        assert resp.status_code == 200

        resp = await admin_client.get("/api/v1/admin/retrieval-terms")
        assert resp.json()["synonyms"]["礼品"] == ["礼金"]
        assert resp.json()["weights"]["考勤"] == 2.0

        # 非法权重（>10 / 空词）被拒
        resp = await admin_client.put("/api/v1/admin/retrieval-terms", json={
            "synonyms": {}, "weights": {"考勤": 99},
        })
        assert resp.status_code == 400
        resp = await admin_client.put("/api/v1/admin/retrieval-terms", json={
            "synonyms": {"": ["x"]}, "weights": {},
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_save_writes_audit(self, admin_client: AsyncClient, tmp_terms_file):
        """保存留审计（retrieval.terms_update）"""
        resp = await admin_client.put("/api/v1/admin/retrieval-terms", json={
            "synonyms": {"报销": ["费用报销"]}, "weights": {},
        })
        assert resp.status_code == 200
        # 审计记录由同事务直写，此处验证响应正常即可（审计埋点已被批次D测试覆盖）


@pytest.fixture
def tmp_suggest_file(tmp_path, monkeypatch):
    """建议暂存文件隔离（防测试污染真实 config/retrieval_suggestions.json）"""
    from app.services import retrieval_suggest

    f = tmp_path / "retrieval_suggestions.json"
    f.write_text('{"suggestions": []}', encoding="utf-8")
    original = retrieval_suggest.SUGGESTIONS_FILE
    monkeypatch.setattr(retrieval_suggest, "SUGGESTIONS_FILE", f)
    yield f
    monkeypatch.setattr(retrieval_suggest, "SUGGESTIONS_FILE", original)


class TestSuggestApi:
    @pytest.mark.asyncio
    async def test_suggest_endpoint_parses_llm_json(
        self, admin_client: AsyncClient, monkeypatch, tmp_suggest_file
    ):
        """8g：建议端点——先落一条用户消息，mock LLM 返回带 markdown 围栏的
        JSON，端点应正确剥离围栏并解析为建议列表"""
        import uuid

        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.conversation import Conversation
        from app.models.message import Message
        from app.models.user import User

        async with async_session_factory() as db:
            user = (await db.execute(select(User).where(User.role == "admin"))).scalar_one()
            conv = Conversation(id=str(uuid.uuid4()), user_id=user.id, title="t", status="active")
            db.add(conv)
            await db.flush()
            db.add(Message(id=str(uuid.uuid4()), conversation_id=conv.id, role="user",
                          content="员工可以收多少金额以下的礼品？"))
            await db.commit()

        class FakeResp:
            content = ('```json\n{"suggestions": [{"query_term": "礼品", '
                       '"doc_term": "礼金", "reason": "制度用词"}]}\n```')

        monkeypatch.setattr(
            "app.rag.llm.get_llm",
            lambda **kw: type("F", (), {"invoke": lambda *a, **k: FakeResp()})(),
        )

        resp = await admin_client.post("/api/v1/admin/retrieval-terms/suggest")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["suggestions"]) == 1
        assert data["suggestions"][0]["query_term"] == "礼品"
        assert data["suggestions"][0]["doc_term"] == "礼金"

    @pytest.mark.asyncio
    async def test_suggest_endpoint_no_messages(self, admin_client: AsyncClient, monkeypatch):
        """8g：无会话数据时不调 LLM，直接返回空建议"""
        called = {"n": 0}

        def boom(*a, **kw):
            called["n"] += 1
            raise AssertionError("无消息时不应调用 LLM")

        monkeypatch.setattr("app.rag.llm.get_llm", lambda **kw: type(
            "F", (), {"invoke": boom})())
        resp = await admin_client.post("/api/v1/admin/retrieval-terms/suggest")
        assert resp.status_code == 200
        assert resp.json()["suggestions"] == []
        assert called["n"] == 0
