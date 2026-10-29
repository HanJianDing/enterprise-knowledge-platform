"""批次A·11f Web 搜索兜底测试：开关/降级/结果解析 + 批次语料增强二期
低相关触发判定（_retrieval_irrelevant）+ 批次F 前置小批限速/缓存"""
import time

import pytest
from langchain_core.documents import Document

from app.rag import web_search
from app.services.chat_service import _retrieval_irrelevant


@pytest.fixture(autouse=True)
def _reset_web_search_state():
    """批次F 前置小批：限速窗口/结果缓存是模块级状态，跨用例会污染
    （同 query"测试问题"前一用例缓存会短路后一用例的 API mock）"""
    web_search._rate_calls.clear()
    web_search._cache.clear()
    yield
    web_search._rate_calls.clear()
    web_search._cache.clear()


class TestRetrievalIrrelevant:
    """批次语料增强二期：低相关判定——检索无分数过滤，仅"无结果"判定
    让 11f 兜底永远点不了火；top1 rerank 分低于阈值时同样触发兜底"""

    def _doc(self, score=None):
        meta = {"filename": "x.txt"}
        if score is not None:
            meta["rerank_score"] = score
        return Document(page_content="内容", metadata=meta)

    def test_low_score_triggers(self):
        assert _retrieval_irrelevant([self._doc(0.08)], None) is True

    def test_normal_score_not_triggers(self):
        assert _retrieval_irrelevant([self._doc(0.5)], None) is False

    def test_exact_threshold_boundary(self):
        """恰等于阈值不触发（< 为严格不等，质检 P3 边界钉住）"""
        assert _retrieval_irrelevant([self._doc(0.15)], None) is False

    def test_no_score_not_triggers(self):
        """未走 rerank 的小候选路径无分数——保守不误触发"""
        assert _retrieval_irrelevant([self._doc(None)], None) is False

    def test_empty_docs_not_triggers(self):
        assert _retrieval_irrelevant([], None) is False

    def test_approval_skipped(self):
        """规则腿确定性通道：审批题检索分数天然低（实测 0.206），
        不得触发 web 兜底"""
        assert _retrieval_irrelevant([self._doc(0.1)], "approval") is False

    def test_relation_skipped(self):
        assert _retrieval_irrelevant([self._doc(0.1)], "relation") is False


class TestSearchWeb:
    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self, monkeypatch):
        """未启用（合规默认）→ 空列表，不发起任何请求"""
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "web_search_enabled", False)
        assert await web_search.search_web("测试问题") == []

    @pytest.mark.asyncio
    async def test_no_key_returns_empty(self, monkeypatch):
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "web_search_enabled", True)
        monkeypatch.setattr(cfg, "web_search_api_key", "")
        assert await web_search.search_web("测试问题") == []

    @pytest.mark.asyncio
    async def test_success_parses_results(self, monkeypatch):
        """成功路径：解析博查返回结构 → [{title,url,snippet}]"""
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "web_search_enabled", True)
        monkeypatch.setattr(cfg, "web_search_api_key", "sk-test-key")

        class FakeResp:
            status_code = 200

            def json(self):
                return {"data": {"webPages": {"value": [
                    {"name": "标题一", "url": "http://a.com",
                     "summary": "摘要一", "snippet": ""},
                    {"name": "标题二", "url": "http://b.com",
                     "summary": "", "snippet": "摘要二"},
                    {"name": "空摘要", "url": "http://c.com",
                     "summary": "", "snippet": ""},
                ]}}}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, *a, **k):
                return FakeResp()

        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        results = await web_search.search_web("测试问题")
        assert len(results) == 2  # 空摘要条目被滤掉
        assert results[0]["title"] == "标题一"
        assert results[0]["snippet"] == "摘要一"
        assert results[1]["snippet"] == "摘要二"

    @pytest.mark.asyncio
    async def test_http_error_returns_empty(self, monkeypatch):
        """HTTP 非 200 → 空列表（静默降级，不影响主链路）"""
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "web_search_enabled", True)
        monkeypatch.setattr(cfg, "web_search_api_key", "sk-test-key")

        class FakeResp:
            status_code = 500
            text = "internal error"

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, *a, **k):
                return FakeResp()

        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        assert await web_search.search_web("测试问题") == []

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self, monkeypatch):
        """网络异常/超时 → 空列表（静默降级）"""
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "web_search_enabled", True)
        monkeypatch.setattr(cfg, "web_search_api_key", "sk-test-key")

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, *a, **k):
                raise TimeoutError("timeout")

        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        assert await web_search.search_web("测试问题") == []


class TestRateLimitAndCache:
    """批次F 前置小批：限速窗口 + 结果缓存（质检 P2 缺口补齐）"""

    def _enable(self, monkeypatch):
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "web_search_enabled", True)
        monkeypatch.setattr(cfg, "web_search_api_key", "sk-test-key")

    @pytest.mark.asyncio
    async def test_rate_limit_returns_empty(self, monkeypatch):
        """窗口内已达每分钟上限 → 空列表，不再出网"""
        self._enable(monkeypatch)
        limit = web_search.settings.web_search_rate_per_min
        web_search._rate_calls[:] = [time.monotonic()] * limit
        called = {"n": 0}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, *a, **k):
                called["n"] += 1
                raise AssertionError("限速后不得发起请求")

        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        assert await web_search.search_web("限速测试") == []
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_cache_hit_skips_api(self, monkeypatch):
        """TTL 内相同 query 直接复用缓存，不调 API"""
        self._enable(monkeypatch)
        web_search._cache["缓存问题"] = (time.monotonic(), [{
            "title": "缓存标题", "url": "http://c.com", "snippet": "缓存摘要"}])
        called = {"n": 0}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, *a, **k):
                called["n"] += 1
                raise AssertionError("缓存命中后不得发起请求")

        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        results = await web_search.search_web("缓存问题")
        assert len(results) == 1
        assert results[0]["title"] == "缓存标题"
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_cache_expired_refetches(self, monkeypatch):
        """缓存过期（>300s）→ 重新调 API"""
        self._enable(monkeypatch)
        web_search._cache["过期问题"] = (
            time.monotonic() - 999, [{"title": "旧", "url": "x", "snippet": "旧"}])

        class FakeResp:
            status_code = 200

            def json(self):
                return {"data": {"webPages": {"value": [
                    {"name": "新标题", "url": "http://n.com",
                     "summary": "新摘要", "snippet": ""}]}}}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, *a, **k):
                return FakeResp()

        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        results = await web_search.search_web("过期问题")
        assert results[0]["title"] == "新标题"

    @pytest.mark.asyncio
    async def test_success_populates_cache(self, monkeypatch):
        """成功调用写入缓存，二次调用命中"""
        self._enable(monkeypatch)

        class FakeResp:
            status_code = 200

            def json(self):
                return {"data": {"webPages": {"value": [
                    {"name": "标题", "url": "http://a.com",
                     "summary": "摘要", "snippet": ""}]}}}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, *a, **k):
                return FakeResp()

        monkeypatch.setattr(web_search.httpx, "AsyncClient", FakeClient)
        first = await web_search.search_web("写缓存问题")
        assert first[0]["title"] == "标题"
        assert "写缓存问题" in web_search._cache
