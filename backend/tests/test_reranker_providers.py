"""验收批次1·1e 测试：重排 provider 抽象（OpenAI 兼容实现 + 降级）"""
from langchain_core.documents import Document

from app.rag.reranker import OpenAICompatReranker, rerank_documents


def _docs(n: int = 3) -> list[Document]:
    return [Document(page_content=f"文档{i}") for i in range(n)]


class TestOpenAICompatReranker:
    def test_request_format_and_ranking(self, monkeypatch):
        """请求体 Cohere 风格；按 relevance_score 降序返回"""
        captured: dict = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"results": [
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.4},
                ]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResp()

        monkeypatch.setattr("httpx.post", fake_post)
        monkeypatch.setattr("app.rag.reranker.settings.rerank_api_base", "http://rerank.internal/v1")
        monkeypatch.setattr("app.rag.reranker.settings.llm_api_key", "k1")
        monkeypatch.setattr("app.rag.reranker.settings.rerank_model", "bge-reranker-base")

        docs = _docs()
        ranked = OpenAICompatReranker().rerank("问题", docs, top_n=2)

        assert captured["url"] == "http://rerank.internal/v1/rerank"
        assert captured["json"]["model"] == "bge-reranker-base"
        assert captured["json"]["query"] == "问题"
        assert captured["json"]["documents"] == ["文档0", "文档1", "文档2"]
        assert captured["headers"]["Authorization"] == "Bearer k1"
        # 按分数降序：index2(0.9) → index0(0.4)
        assert [d.page_content for d in ranked] == ["文档2", "文档0"]
        # 批次语料增强二期：加权分数写进 metadata（低相关兜底判定的依据）
        assert ranked[0].metadata["rerank_score"] == 0.9
        assert ranked[1].metadata["rerank_score"] == 0.4

    def test_dashscope_style_response_parsed(self, monkeypatch):
        """dashscope 兼容风格的响应结构（output.results）也能解析"""
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"output": {"results": [
                    {"index": 1, "relevance_score": 0.7},
                ]}}

        monkeypatch.setattr("httpx.post", lambda *a, **kw: FakeResp())
        monkeypatch.setattr("app.rag.reranker.settings.rerank_api_base", "http://x/v1")
        monkeypatch.setattr("app.rag.reranker.settings.llm_api_key", "")
        monkeypatch.setattr("app.rag.reranker.settings.dashscope_api_key", "dk")

        docs = _docs()
        ranked = OpenAICompatReranker().rerank("问题", docs, top_n=1)
        assert [d.page_content for d in ranked] == ["文档1"]
        assert ranked[0].metadata["rerank_score"] == 0.7

    def test_boost_multiplies_attached_score(self, monkeypatch):
        """批次语料增强二期：boosts（时效衰减）乘过的分数才是排序依据，
        metadata 里存的必须是与排序一致的加权分"""
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"results": [
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 1, "relevance_score": 0.8},
                ]}

        monkeypatch.setattr("httpx.post", lambda *a, **kw: FakeResp())
        monkeypatch.setattr("app.rag.reranker.settings.rerank_api_base", "http://x/v1")

        docs = [Document(page_content="甲", metadata={"document_id": "d1"}),
                Document(page_content="乙", metadata={"document_id": "d2"})]
        # d1 衰减系数 0.2（过期通知）：0.5×0.2=0.1 < 0.8 → d2 排第一
        ranked = OpenAICompatReranker().rerank("问题", docs, top_n=2,
                                               boosts={"d1": 0.2})
        assert [d.page_content for d in ranked] == ["乙", "甲"]
        assert ranked[0].metadata["rerank_score"] == 0.8
        assert ranked[1].metadata["rerank_score"] == round(0.5 * 0.2, 6)

    def test_failure_falls_back_original_order(self, monkeypatch):
        """请求异常降级为原顺序前 top_n"""
        def boom(*a, **kw):
            raise RuntimeError("网络错误")

        monkeypatch.setattr("httpx.post", boom)
        monkeypatch.setattr("app.rag.reranker.settings.rerank_api_base", "http://x/v1")

        docs = _docs()
        ranked = OpenAICompatReranker().rerank("问题", docs, top_n=2)
        assert [d.page_content for d in ranked] == ["文档0", "文档1"]

    def test_missing_base_falls_back(self, monkeypatch):
        """未配置端点直接降级"""
        monkeypatch.setattr("app.rag.reranker.settings.rerank_api_base", "")
        docs = _docs()
        ranked = OpenAICompatReranker().rerank("问题", docs, top_n=2)
        assert len(ranked) == 2


class TestRerankFactory:
    def test_single_doc_shortcut(self, monkeypatch):
        """单条候选直接返回，不调 provider"""
        called = {"n": 0}

        def spy(query, docs, top_n):
            called["n"] += 1
            return docs

        monkeypatch.setattr("app.rag.reranker._get_reranker",
                            lambda: type("F", (), {"rerank": spy})())
        docs = _docs(1)
        assert rerank_documents("q", docs) == docs
        assert called["n"] == 0
