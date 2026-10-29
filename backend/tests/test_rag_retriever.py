"""RAG 检索器测试 — 针对可独立测试的组件"""
import pytest
from unittest.mock import patch, MagicMock
from langchain_core.documents import Document

from app.rag.retriever import BM25Retriever, get_retriever


# ════════════════════════════════════════════════════
# BM25Retriever 测试
# ════════════════════════════════════════════════════

class TestBM25Retriever:
    """BM25 关键词检索器测试"""

    @pytest.fixture
    def sample_docs(self):
        """创建测试文档集"""
        return [
            Document(
                page_content="iPhone 15 Pro Max 电池容量为 4422mAh",
                metadata={"doc_id": "1", "category_id": "phone"},
            ),
            Document(
                page_content="华为 Mate 60 Pro 电池容量为 4750mAh",
                metadata={"doc_id": "2", "category_id": "phone"},
            ),
            Document(
                page_content="AirPods Pro 充电盒容量为 500mAh",
                metadata={"doc_id": "3", "category_id": "audio"},
            ),
            Document(
                page_content="小米 14 Pro 搭载徕卡光学镜头",
                metadata={"doc_id": "4", "category_id": "phone"},
            ),
        ]

    @pytest.fixture
    def retriever(self, sample_docs):
        """创建 BM25 检索器实例"""
        return BM25Retriever(sample_docs, top_k=3)

    def test_creation_with_documents(self, retriever):
        """测试检索器正常创建"""
        assert retriever is not None
        assert retriever._top_k == 3
        assert len(retriever._docs) == 4

    def test_retrieve_relevant_documents(self, retriever):
        """测试相关文档检索"""
        results = retriever._get_relevant_documents("iPhone 电池")
        assert len(results) > 0
        assert len(results) <= 3  # top_k=3
        # 最相关的结果应包含 iPhone
        assert "iPhone" in results[0].page_content

    def test_retrieve_chinese_query(self, retriever):
        """测试中文查询"""
        results = retriever._get_relevant_documents("华为手机电池")
        assert len(results) > 0
        # 至少有一个结果包含 华为
        assert any("华为" in r.page_content for r in results)

    def test_retrieve_no_match(self, retriever):
        """测试无匹配查询"""
        results = retriever._get_relevant_documents("xyz不存在的关键词abc")
        # 可能返回空（得分都为0时）或返回部分（某些词碰巧匹配）
        assert len(results) <= 3

    def test_retrieve_empty_query(self, retriever):
        """测试空查询"""
        results = retriever._get_relevant_documents("")
        # 空查询不应报错
        assert isinstance(results, list)

    def test_top_k_respected(self, sample_docs):
        """测试 top_k 参数被遵守"""
        retriever_top1 = BM25Retriever(sample_docs, top_k=1)
        results = retriever_top1._get_relevant_documents("电池")
        assert len(results) <= 1

        retriever_top4 = BM25Retriever(sample_docs, top_k=4)
        results = retriever_top4._get_relevant_documents("电池")
        assert len(results) <= 4

    def test_metadata_preserved(self, retriever):
        """测试检索结果保留原始 metadata"""
        results = retriever._get_relevant_documents("小米")
        if results:
            doc = results[0]
            assert "doc_id" in doc.metadata
            assert "category_id" in doc.metadata
            assert doc.metadata["doc_id"] == "4"

    def test_single_document_retriever(self):
        """测试单文档检索器"""
        single_doc = [Document(page_content="iPhone 15 Pro Max 产品规格说明书", metadata={"id": "1"})]
        retriever = BM25Retriever(single_doc, top_k=3)
        results = retriever._get_relevant_documents("iPhone 产品")
        # 单文档时如查询匹配应返回结果
        assert len(results) <= 1

    def test_empty_documents_no_crash(self):
        """空文档列表不崩溃、返回空结果（验收批次1·8a：自写打分替代 rank_bm25，
        旧实现空语料抛 ZeroDivisionError，新实现优雅降级）"""
        retriever = BM25Retriever([], top_k=3)
        assert retriever.invoke("任意查询") == []


# ════════════════════════════════════════════════════
# get_retriever 测试 (需要 mock ChromaDB)
# ════════════════════════════════════════════════════

class TestGetRetriever:
    """向量检索器获取测试（mock 模式）"""

    @patch("app.rag.retriever.get_vector_store")
    def test_get_retriever_basic(self, mock_store):
        """测试基本检索器获取"""
        mock_vs = MagicMock()
        mock_store.return_value = mock_vs
        # 分块统计要给出非空 ids，否则走"空集合"分支返回空检索器
        mock_vs._collection.get.return_value = {"ids": [f"c{i}" for i in range(20)]}

        retriever = get_retriever(top_k=10, doc_ids=["d1"])
        assert retriever is not None
        # 验证 as_retriever 被调用
        mock_vs.as_retriever.assert_called_once()

    @patch("app.rag.retriever.get_vector_store")
    def test_get_retriever_requires_doc_ids(self, mock_store):
        """企业知识库阶段1：不传可见文档范围直接报错（fail-closed 防越权）"""
        mock_vs = MagicMock()
        mock_store.return_value = mock_vs

        with pytest.raises(ValueError):
            get_retriever(top_k=10)
        with pytest.raises(ValueError):
            get_retriever(top_k=10, category_ids=["cat_1"])

    @patch("app.rag.retriever.get_vector_store")
    def test_get_retriever_with_category_filter(self, mock_store):
        """测试带分类过滤的检索器获取（文档范围与分类过滤经 $and 组合）"""
        mock_vs = MagicMock()
        mock_store.return_value = mock_vs
        # B15 修复后 get_retriever 会先统计过滤后分块数（为空则返回空检索器），
        # mock 需要给出足够数量的 ids 才能走到 as_retriever 分支
        mock_vs._collection.get.return_value = {"ids": [f"c{i}" for i in range(20)]}

        doc_ids = ["d1", "d2"]
        category_ids = ["cat_1", "cat_2"]
        retriever = get_retriever(top_k=6, doc_ids=doc_ids, category_ids=category_ids)
        assert retriever is not None
        # 验证传入的 filter 参数：$and = [文档范围, 分类]
        call_kwargs = mock_vs.as_retriever.call_args[1]
        assert "filter" in call_kwargs["search_kwargs"]
        filter_dict = call_kwargs["search_kwargs"]["filter"]
        assert "$and" in filter_dict
        assert filter_dict["$and"][0]["document_id"]["$in"] == doc_ids
        assert filter_dict["$and"][1]["category_id"]["$in"] == category_ids

    @patch("app.rag.retriever.get_vector_store")
    def test_get_retriever_search_type_mmr(self, mock_store):
        """测试使用 MMR 搜索类型"""
        mock_vs = MagicMock()
        mock_store.return_value = mock_vs
        mock_vs._collection.get.return_value = {"ids": [f"c{i}" for i in range(20)]}

        get_retriever(top_k=5, doc_ids=["d1"])
        call_kwargs = mock_vs.as_retriever.call_args[1]
        assert call_kwargs["search_type"] == "mmr"

    @patch("app.rag.retriever.get_vector_store")
    def test_get_retriever_fetch_k_larger_than_k(self, mock_store):
        """测试 fetch_k > k（MMR 需要更大的候选集）"""
        mock_vs = MagicMock()
        mock_store.return_value = mock_vs
        mock_vs._collection.get.return_value = {"ids": [f"c{i}" for i in range(20)]}

        get_retriever(top_k=6, doc_ids=["d1"])
        call_kwargs = mock_vs.as_retriever.call_args[1]
        assert call_kwargs["search_kwargs"]["fetch_k"] >= call_kwargs["search_kwargs"]["k"]
        assert call_kwargs["search_kwargs"]["fetch_k"] == 18  # top_k * 3


# ════════════════════════════════════════════════════
# 混合检索 RRF 融合回归测试（P1-2/P1-3 修复）
# ════════════════════════════════════════════════════

class _FakeRetriever:
    """返回固定文档集的假检索器"""

    def __init__(self, docs):
        self._docs = docs

    def invoke(self, query, **kwargs):
        return self._docs


_SAME_PREFIX = "iPhone 15 Pro 详细参数说明。" * 6  # 21 字 × 6 = 126 字符，保证前 100+ 字符相同


class TestHybridMerge:
    """get_hybrid_retriever 的去重与排序"""

    def _make_hybrid(self, vector_docs, bm25_docs, top_k=6, doc_ids=None):
        # 企业知识库阶段1：doc_ids 为可见文档范围（BM25 内存过滤依据）；
        # 测试文档的 document_id 是 d1~d4，默认全部可见
        if doc_ids is None:
            doc_ids = ["d1", "d2", "d3", "d4"]
        with patch("app.rag.retriever.get_retriever", return_value=_FakeRetriever(vector_docs)), \
             patch("app.rag.retriever._build_bm25_retriever", return_value=_FakeRetriever(bm25_docs)):
            from app.rag.retriever import get_hybrid_retriever
            return get_hybrid_retriever(top_k=top_k, doc_ids=doc_ids)

    def test_same_prefix_different_chunks_not_merged(self):
        """修复：前 100 字符相同的不同分块（不同 chunk_index）不再被误合并"""
        doc_a = Document(page_content=_SAME_PREFIX + "A款内容", metadata={"document_id": "d1", "chunk_index": 0})
        doc_b = Document(page_content=_SAME_PREFIX + "B款内容", metadata={"document_id": "d1", "chunk_index": 1})

        # 自我校验：两条文档前 100 字符确实相同（旧实现按 page_content[:100] 去重会误合并）
        assert len(_SAME_PREFIX) >= 100
        assert doc_a.page_content[:100] == doc_b.page_content[:100]

        hybrid = self._make_hybrid([doc_a], [doc_b])
        results = hybrid.invoke("iPhone 15 Pro")
        assert len(results) == 2  # 旧实现按 page_content[:100] 去重，只会剩 1 条

    def test_same_chunk_from_both_routes_deduped(self):
        """同一分块（document_id+chunk_index 相同）在向量与 BM25 两路都出现 → 只保留一条"""
        doc = Document(page_content="华为 Mate 80 电池容量", metadata={"document_id": "d2", "chunk_index": 3})
        dup = Document(page_content="华为 Mate 80 电池容量", metadata={"document_id": "d2", "chunk_index": 3})

        hybrid = self._make_hybrid([doc], [dup])
        results = hybrid.invoke("华为电池")
        assert len(results) == 1

    def test_rrf_ranking_prefers_double_hit(self):
        """两路都命中的分块排在只在一路命中的分块前面"""
        hit_both = Document(page_content="尺码表说明", metadata={"document_id": "d3", "chunk_index": 0})
        only_vector = Document(page_content="退换货政策", metadata={"document_id": "d4", "chunk_index": 0})

        hybrid = self._make_hybrid([only_vector, hit_both], [hit_both])
        results = hybrid.invoke("尺码")
        assert results[0].metadata["document_id"] == "d3"


class TestBM25Cache:
    """P2-1 回归：BM25 缓存命中不再全量拉取；失效后重建"""

    def _make_store_mock(self):
        collection = MagicMock()
        collection.count.return_value = 1
        collection.get.return_value = {
            "documents": ["华为 Mate 80 电池容量 4750mAh"],
            "metadatas": [{"document_id": "d1", "chunk_index": 0}],
        }
        store = MagicMock()
        store._collection = collection
        return store, collection

    def test_cache_hit_skips_full_scan(self):
        """构建一次后，第二次调用不再 collection.get（只做 count 轻量检查）"""
        import app.rag.retriever as R
        store, collection = self._make_store_mock()
        with patch.object(R, "get_vector_store", return_value=store):
            try:
                R._bm25_retriever = None
                R._bm25_dirty = True
                first = R._build_bm25_retriever()
                assert first is not None
                assert collection.get.call_count == 1
                second = R._build_bm25_retriever()
                assert second is first  # 命中缓存返回同一对象
                assert collection.get.call_count == 1  # 没有再全量拉取
            finally:
                # 还原模块级缓存状态，避免影响后续真实向量库的集成测试
                R._bm25_retriever = None
                R._bm25_dirty = True

    def test_invalidate_triggers_rebuild(self):
        """文档变更（_invalidate_bm25）后，下次调用重新全量构建"""
        import app.rag.retriever as R
        store, collection = self._make_store_mock()
        with patch.object(R, "get_vector_store", return_value=store):
            try:
                R._bm25_retriever = None
                R._bm25_dirty = True
                R._build_bm25_retriever()
                R._invalidate_bm25()
                R._build_bm25_retriever()
                assert collection.get.call_count == 2  # 失效后全量重建
            finally:
                R._bm25_retriever = None
                R._bm25_dirty = True


class TestNotifyDecay:
    """验收批次1·8i：通知/公告时效衰减系数（时态感知检索）"""

    def test_fresh_notice_full_weight(self):
        from app.rag.retriever import notify_decay_factor
        assert notify_decay_factor(0) == 1.0
        assert notify_decay_factor(90) == 1.0

    def test_year_old_reduced(self):
        from app.rag.retriever import notify_decay_factor
        assert notify_decay_factor(91) == 0.7
        assert notify_decay_factor(365) == 0.7

    def test_stale_notice_heavily_reduced(self):
        from app.rag.retriever import notify_decay_factor
        assert notify_decay_factor(366) == 0.4
        assert notify_decay_factor(1000) == 0.4
