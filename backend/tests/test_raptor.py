"""批次11a RAPTOR 摘要树测试：聚类 / 建树循环 / 降级 / 展开 / 清理"""
import pytest
from langchain_core.documents import Document

from app.rag import raptor
from app.rag.raptor import _SUMMARY_INDEX_BASE, cluster_chunks, _Node


# ── 聚类（真实 UMAP+HDBSCAN）──


class TestClusterChunks:
    def test_too_few_returns_single_cluster(self):
        assert cluster_chunks([[0.1] * 8]) == [[0]]
        assert cluster_chunks([[0.1] * 8, [0.2] * 8]) == [[0, 1]]

    def test_separated_groups_split_and_cover_all(self):
        """两组距离极远的向量 → 稳定分成 2 簇（可能外加杂项簇）；
        硬断言：每个下标恰好出现一次（覆盖性+无重复）"""
        vecs = []
        for i in range(6):
            vecs.append([0.0] * 7 + [i * 0.001])   # 组 A：原点附近
        for i in range(6):
            vecs.append([1.0] * 7 + [i * 0.001])   # 组 B：远点附近
        clusters = cluster_chunks(vecs)
        assert len(clusters) >= 2
        seen = [i for c in clusters for i in c]
        assert sorted(seen) == list(range(12))
        # 两个大组必须分属不同簇（组内同簇）
        group_of = {}
        for ci, c in enumerate(clusters):
            for i in c:
                group_of[i] = ci
        assert group_of[0] == group_of[5]
        assert group_of[6] == group_of[11]
        assert group_of[0] != group_of[6]

    def test_noise_points_gathered_into_misc_cluster(self):
        """远离任何簇的孤立点被 HDBSCAN 判噪声 → 合成杂项簇（RAPTOR 原文做法），
        不丢信息（每个点仍恰好出现一次）"""
        vecs = [[0.1] * 8] * 6 + [[10.0] * 8] + [[-10.0] * 8]
        clusters = cluster_chunks(vecs)
        seen = sorted(i for c in clusters for i in c)
        assert seen == list(range(8))


# ── 建树（mock 嵌入/聚类/LLM，真实 SQLite 簇表）──


def _fake_leaf_pool(n: int = 12) -> list[_Node]:
    return [_Node(content=f"叶子块{i}的内容" + "制度条款内容" * 5,
                  chunk_index=i) for i in range(n)]


class TestBuildTree:
    @pytest.mark.asyncio
    async def test_build_two_levels_and_persist(self, monkeypatch):
        """12 叶子 → mock 2 簇 → 层1 两摘要块 → 层2 单簇终止；
        断言：摘要块 metadata / chunk_index 偏移 / 簇表行"""
        import app.rag.raptor as raptor_mod
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "raptor_enabled", True)
        monkeypatch.setattr(cfg, "raptor_min_chars", 100)  # 测试语料短，压低阈值
        monkeypatch.setattr(
            raptor_mod, "_fetch_leaf_pool", lambda doc_id: _fake_leaf_pool()
        )
        # 嵌入：返回固定向量（无需真实嵌入 API）
        class FakeEmbeddings:
            def embed_documents(self, texts):
                return [[0.1] * 8 for _ in texts]

        monkeypatch.setattr(
            "app.rag.embeddings.get_embeddings", lambda: FakeEmbeddings()
        )
        # 聚类：首次 2 簇（0-5 / 6-11），第二次（层2 只剩 2 节点）返回单簇 → 终止
        call_no = {"n": 0}

        def fake_cluster(embeddings):
            call_no["n"] += 1
            if call_no["n"] == 1:
                return [list(range(6)), list(range(6, 12))]
            return [list(range(len(embeddings)))]

        monkeypatch.setattr(raptor_mod, "cluster_chunks", fake_cluster)
        # LLM 摘要：确定性返回
        async def fake_summarize(doc_name, level, texts):
            return f"{doc_name}第{level}层摘要内容"

        monkeypatch.setattr(raptor_mod, "_summarize_cluster", fake_summarize)
        # Chroma 读写与写入拦截（_clear_existing 的 get 返回空；写入记录到 stored）
        stored: list[Document] = []

        class FakeCollection:
            def get(self, where=None, include=None):
                return {"ids": [], "documents": [], "metadatas": []}

            def delete(self, ids=None, where=None):
                return None

        class FakeStore:
            _collection = FakeCollection()

        monkeypatch.setattr(
            "app.rag.retriever.get_vector_store", lambda: FakeStore()
        )
        # add_documents_to_store 在函数内 from app.rag.retriever import——
        # patch retriever 模块属性
        import app.rag.retriever as retriever_mod

        def fake_add(docs):
            stored.extend(docs)

        monkeypatch.setattr(retriever_mod, "add_documents_to_store", fake_add)

        result = await raptor.build_tree_for_document("doc-1", "厚文档.txt")

        assert result is not None
        assert result["levels"] == 1        # 层 1 建完、层 2 单簇终止
        assert result["clusters"] == 2
        assert result["summaries"] == 2
        assert len(stored) == 2
        for doc, ci in zip(stored, range(2)):
            meta = doc.metadata
            assert meta["chunk_type"] == "raptor_summary"
            assert meta["tree_level"] == 1
            assert meta["cluster_id"] == f"L1C{ci}"
            assert meta["document_id"] == "doc-1"
            assert meta["chunk_index"] >= _SUMMARY_INDEX_BASE  # 独立编号空间
            assert "摘要" in doc.page_content and "厚文档.txt" in doc.page_content

        # 簇表落库（真实 SQLite）：两行、child 列表正确
        from app.infrastructure.database import async_session_factory
        from app.models.raptor_cluster import RaptorCluster
        from sqlalchemy import select

        async with async_session_factory() as db:
            rows = (await db.execute(select(RaptorCluster))).scalars().all()
        assert len(rows) == 2
        by_cluster = {r.cluster_id: r for r in rows}
        assert by_cluster["L1C0"].child_chunk_indices == list(range(6))
        assert by_cluster["L1C1"].child_chunk_indices == list(range(6, 12))
        assert all(r.summary_chunk_index >= _SUMMARY_INDEX_BASE for r in rows)

    @pytest.mark.asyncio
    async def test_rebuild_is_idempotent(self, monkeypatch):
        """幂等：重建前清旧摘要块与旧簇行，连建两次行数不翻倍"""
        import app.rag.raptor as raptor_mod
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "raptor_min_chars", 100)
        monkeypatch.setattr(
            raptor_mod, "_fetch_leaf_pool", lambda doc_id: _fake_leaf_pool()
        )

        class FakeEmbeddings:
            def embed_documents(self, texts):
                return [[0.1] * 8 for _ in texts]

        monkeypatch.setattr(
            "app.rag.embeddings.get_embeddings", lambda: FakeEmbeddings()
        )

        def fake_cluster(embeddings):
            n = len(embeddings)
            if n > 2:
                return [list(range(6)), list(range(6, 12))]
            return [list(range(n))]  # 上层只剩摘要节点时单簇 → 终止

        monkeypatch.setattr(raptor_mod, "cluster_chunks", fake_cluster)

        async def fake_summarize(doc_name, level, texts):
            return "摘要"

        monkeypatch.setattr(raptor_mod, "_summarize_cluster", fake_summarize)
        import app.rag.retriever as retriever_mod

        monkeypatch.setattr(retriever_mod, "add_documents_to_store", lambda docs: None)

        class FakeCollection:
            def get(self, where=None, include=None):
                return {"ids": [], "documents": [], "metadatas": []}

            def delete(self, ids=None, where=None):
                return None

        class FakeStore:
            _collection = FakeCollection()

        monkeypatch.setattr(
            "app.rag.retriever.get_vector_store", lambda: FakeStore()
        )

        await raptor.build_tree_for_document("doc-2", "厚文档.txt")
        await raptor.build_tree_for_document("doc-2", "厚文档.txt")

        from app.infrastructure.database import async_session_factory
        from app.models.raptor_cluster import RaptorCluster
        from sqlalchemy import select

        async with async_session_factory() as db:
            rows = (await db.execute(
                select(RaptorCluster).where(RaptorCluster.document_id == "doc-2")
            )).scalars().all()
        assert len(rows) == 2  # 不翻倍

    @pytest.mark.asyncio
    async def test_skip_when_too_small(self, monkeypatch):
        """叶子不足/字数不够 → 跳过建树（不烧 LLM）"""
        import app.rag.raptor as raptor_mod

        monkeypatch.setattr(
            raptor_mod, "_fetch_leaf_pool", lambda doc_id: _fake_leaf_pool(n=4)
        )
        called = {"n": 0}

        async def fake_summarize(*a, **k):
            called["n"] += 1
            return "摘要"

        monkeypatch.setattr(raptor_mod, "_summarize_cluster", fake_summarize)
        result = await raptor.build_tree_for_document("doc-3", "薄文档.txt")
        assert result is None
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_summary_failure_does_not_crash(self, monkeypatch):
        """LLM 摘要全失败 → 成员上浮、不写摘要块；函数不抛异常（降级）"""
        import app.rag.raptor as raptor_mod
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "raptor_min_chars", 100)
        monkeypatch.setattr(
            raptor_mod, "_fetch_leaf_pool", lambda doc_id: _fake_leaf_pool()
        )

        class FakeEmbeddings:
            def embed_documents(self, texts):
                return [[0.1] * 8 for _ in texts]

        monkeypatch.setattr(
            "app.rag.embeddings.get_embeddings", lambda: FakeEmbeddings()
        )

        def fake_cluster(embeddings):
            n = len(embeddings)
            if n > 2:
                return [list(range(6)), list(range(6, 12))]
            return [list(range(n))]

        monkeypatch.setattr(raptor_mod, "cluster_chunks", fake_cluster)

        async def fake_summarize(*a, **k):
            return None  # 模拟 LLM 失败

        monkeypatch.setattr(raptor_mod, "_summarize_cluster", fake_summarize)
        import app.rag.retriever as retriever_mod

        monkeypatch.setattr(retriever_mod, "add_documents_to_store", lambda docs: None)

        class FakeCollection:
            def get(self, where=None, include=None):
                return {"ids": [], "documents": [], "metadatas": []}

            def delete(self, ids=None, where=None):
                return None

        class FakeStore:
            _collection = FakeCollection()

        monkeypatch.setattr(
            "app.rag.retriever.get_vector_store", lambda: FakeStore()
        )

        result = await raptor.build_tree_for_document("doc-4", "厚文档.txt")
        # 摘要全失败 → 无有效簇行 → 返回 None（不抛异常、不写簇表）
        assert result is None


# ── 展开（mock Chroma，真实 SQLite 簇表）──


class TestExpandSummaryChildren:
    @pytest.mark.asyncio
    async def test_expand_leaf_children(self, monkeypatch):
        """命中摘要块 → 展开子树叶子追加；层 2 摘要递归下钻到叶子"""
        from app.infrastructure.database import async_session_factory
        from app.models.raptor_cluster import RaptorCluster

        async with async_session_factory() as db:
            db.add_all([
                # 层 1 两簇：各自 3 个叶子
                RaptorCluster(document_id="doc-5", tree_level=1, cluster_id="L1C0",
                              summary_chunk_index=_SUMMARY_INDEX_BASE + 1000,
                              child_chunk_indices=[0, 1, 2]),
                RaptorCluster(document_id="doc-5", tree_level=1, cluster_id="L1C1",
                              summary_chunk_index=_SUMMARY_INDEX_BASE + 1001,
                              child_chunk_indices=[3, 4, 5]),
                # 层 2 一簇：成员是层 1 摘要块 → 递归下钻
                RaptorCluster(document_id="doc-5", tree_level=2, cluster_id="L2C0",
                              summary_chunk_index=_SUMMARY_INDEX_BASE + 2000,
                              child_chunk_indices=[
                                  _SUMMARY_INDEX_BASE + 1000,
                                  _SUMMARY_INDEX_BASE + 1001,
                              ]),
            ])
            await db.commit()

            # mock Chroma：按 document_id 返回 6 个叶子块
            class FakeCollection:
                def get(self, where=None, include=None):
                    idxs = range(6)
                    return {
                        "documents": [f"叶子{i}内容" for i in idxs],
                        "metadatas": [
                            {"document_id": "doc-5", "chunk_index": i,
                             "chunk_type": "long_text"}
                            for i in idxs
                        ],
                    }

            class FakeStore:
                _collection = FakeCollection()

            monkeypatch.setattr(
                "app.rag.retriever.get_vector_store", lambda: FakeStore()
            )

            # 命中层 2 摘要块 → 递归展开到叶子（上限 3）
            hit = Document(
                page_content="摘要内容",
                metadata={"document_id": "doc-5", "chunk_type": "raptor_summary",
                          "tree_level": 2, "cluster_id": "L2C0",
                          "chunk_index": _SUMMARY_INDEX_BASE + 2000},
            )
            out = await raptor.expand_summary_children(db, [hit])
            assert len(out) == 1 + 3  # 命中块 + 3 个展开叶子
            extra = out[1:]
            leaf_idxs = sorted(d.metadata["chunk_index"] for d in extra)
            assert leaf_idxs == [0, 1, 2]  # 下钻取到层 1 簇的首批叶子

    @pytest.mark.asyncio
    async def test_no_summary_hit_returns_unchanged(self, monkeypatch):
        """无摘要块命中 → 原样返回（零开销）"""
        from app.infrastructure.database import async_session_factory

        async with async_session_factory() as db:
            docs = [Document(page_content="普通叶子", metadata={
                "document_id": "d", "chunk_index": 0, "chunk_type": "long_text"})]
            out = await raptor.expand_summary_children(db, docs)
            assert out is docs


# ── 树遍历检索（批次B）──


class TestTreeTraversal:
    def _setup_cache_and_store(self, monkeypatch):
        """构造两棵树（2 层/1 层）+ 假 Chroma（摘要块向量+叶子块）+ 假嵌入"""
        import app.rag.raptor as raptor_mod

        # doc-7 两棵树数据：L2C0 的 child 是 L1C0/L1C1；L1C0 叶子 0,1；L1C1 叶子 2,3
        raptor_mod._tree_cache = {
            "doc-7": {
                "max_level": 2,
                "clusters": [
                    {"level": 1, "cluster_id": "L1C0",
                     "summary_chunk_index": _SUMMARY_INDEX_BASE + 1000,
                     "child_chunk_indices": [0, 1]},
                    {"level": 1, "cluster_id": "L1C1",
                     "summary_chunk_index": _SUMMARY_INDEX_BASE + 1001,
                     "child_chunk_indices": [2, 3]},
                    {"level": 2, "cluster_id": "L2C0",
                     "summary_chunk_index": _SUMMARY_INDEX_BASE + 2000,
                     "child_chunk_indices": [_SUMMARY_INDEX_BASE + 1000,
                                             _SUMMARY_INDEX_BASE + 1001]},
                ],
            },
            "doc-8": {
                "max_level": 1,
                "clusters": [
                    {"level": 1, "cluster_id": "L1C0",
                     "summary_chunk_index": _SUMMARY_INDEX_BASE + 1000,
                     "child_chunk_indices": [10, 11]},
                ],
            },
        }

        import numpy as np

        class FakeCollection:
            def get(self, where=None, include=None):
                doc_id = (where or {}).get("document_id")
                if doc_id == "doc-7":
                    return {
                        "documents": [f"叶子{i}" for i in range(4)]
                                     + ["摘要A", "摘要B", "顶层摘要"],
                        "metadatas": [
                            {"document_id": "doc-7", "chunk_index": i,
                             "chunk_type": "long_text"} for i in range(4)
                        ] + [
                            {"document_id": "doc-7",
                             "chunk_index": _SUMMARY_INDEX_BASE + 1000,
                             "chunk_type": "raptor_summary", "tree_level": 1},
                            {"document_id": "doc-7",
                             "chunk_index": _SUMMARY_INDEX_BASE + 1001,
                             "chunk_type": "raptor_summary", "tree_level": 1},
                            {"document_id": "doc-7",
                             "chunk_index": _SUMMARY_INDEX_BASE + 2000,
                             "chunk_type": "raptor_summary", "tree_level": 2},
                        ],
                        "embeddings": [
                            [0.9, 0.1], [0.8, 0.2], [0.1, 0.9], [0.2, 0.8],
                            [0.85, 0.15], [0.15, 0.85], [0.5, 0.5],
                        ],
                    }
                if doc_id == "doc-8":
                    return {
                        "documents": ["叶子10", "叶子11", "摘要C"],
                        "metadatas": [
                            {"document_id": "doc-8", "chunk_index": 10,
                             "chunk_type": "long_text"},
                            {"document_id": "doc-8", "chunk_index": 11,
                             "chunk_type": "long_text"},
                            {"document_id": "doc-8",
                             "chunk_index": _SUMMARY_INDEX_BASE + 1000,
                             "chunk_type": "raptor_summary", "tree_level": 1},
                        ],
                        "embeddings": [
                            [0.9, 0.1], [0.8, 0.2], [0.85, 0.15],
                        ],
                    }
                return {"documents": [], "metadatas": [], "embeddings": []}

        class FakeStore:
            _collection = FakeCollection()

        monkeypatch.setattr(
            "app.rag.retriever.get_vector_store", lambda: FakeStore()
        )

        class FakeEmbeddings:
            def embed_query(self, query):
                return [0.9, 0.1]  # 与"摘要A/L1C0 分支"最相似

        monkeypatch.setattr(
            "app.rag.embeddings.get_embeddings", lambda: FakeEmbeddings()
        )
        return raptor_mod

    def test_traversal_drills_down_most_similar_branch(self, monkeypatch):
        """顶层选最相关簇 → 下钻到其叶子；只收集所选分支"""
        import app.rag.raptor as raptor_mod
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "raptor_traversal_enabled", True)
        self._setup_cache_and_store(monkeypatch)
        docs = raptor_mod.tree_traversal_search(
            "测试", ["doc-7", "doc-8"], top_clusters_per_level=1
        )
        leaf_keys = sorted(
            (d.metadata["document_id"], d.metadata["chunk_index"]) for d in docs
        )
        # doc-7：L2C0 下钻 → L1C0（最相似）→ 叶子 0,1；doc-8 → 叶子 10,11
        assert ("doc-7", 0) in leaf_keys
        assert ("doc-7", 1) in leaf_keys
        assert ("doc-8", 10) in leaf_keys
        assert ("doc-8", 11) in leaf_keys

    def test_traversal_disabled_returns_empty(self, monkeypatch):
        import app.rag.raptor as raptor_mod
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "raptor_traversal_enabled", False)
        assert raptor_mod.tree_traversal_search("测试", ["doc-7"]) == []

    def test_traversal_no_cache_returns_empty(self, monkeypatch):
        import app.rag.raptor as raptor_mod
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "raptor_traversal_enabled", True)
        raptor_mod._tree_cache = {}
        assert raptor_mod.tree_traversal_search("测试", ["doc-7"]) == []

    @pytest.mark.asyncio
    async def test_refresh_builds_cache_and_invalidate(self, monkeypatch):
        """refresh_tree_cache 从簇表构建缓存；invalidate（代数递增）后重建；
        命中时零成本（代数未变直接返回）"""
        import app.rag.raptor as raptor_mod
        from app.infrastructure.database import async_session_factory
        from app.models.raptor_cluster import RaptorCluster

        raptor_mod._tree_cache = {}
        raptor_mod.invalidate_tree_cache()  # 代数 +1，触发重建
        try:
            async with async_session_factory() as db:
                db.add(RaptorCluster(document_id="doc-9", tree_level=1,
                                     cluster_id="L1C0",
                                     summary_chunk_index=_SUMMARY_INDEX_BASE + 1000,
                                     child_chunk_indices=[0, 1]))
                await db.commit()
                await raptor_mod.refresh_tree_cache(db)
            assert "doc-9" in raptor_mod._tree_cache
            assert raptor_mod._tree_cache["doc-9"]["max_level"] == 1

            # 代数未变：refresh 命中零成本（缓存不重建）
            gen = raptor_mod._tree_generation_seen
            async with async_session_factory() as db:
                await raptor_mod.refresh_tree_cache(db)
            assert raptor_mod._tree_generation_seen == gen

            # invalidate 后代数递增 → 重建
            raptor_mod.invalidate_tree_cache()
            async with async_session_factory() as db:
                await raptor_mod.refresh_tree_cache(db)
            assert "doc-9" in raptor_mod._tree_cache
        finally:
            # 批次B.5 P3-2：清理直插簇表行（防残留污染其他用例）；
            # 同步置脏缓存（缓存里可能有本用例写入的结构残留）
            from sqlalchemy import delete

            async with async_session_factory() as db:
                await db.execute(delete(RaptorCluster).where(
                    RaptorCluster.document_id == "doc-9"))
                await db.commit()
            raptor_mod.invalidate_tree_cache()

    @pytest.mark.asyncio
    async def test_first_refresh_from_initial_state_builds_cache(self, monkeypatch):
        """批次B 补审 P1 回归钉住：纯初态（无预热 invalidate）首次 refresh
        必须建缓存——初值 seen=-1 ≠ generation=0，早退条件不成立"""
        import app.rag.raptor as raptor_mod
        from app.infrastructure.database import async_session_factory
        from app.models.raptor_cluster import RaptorCluster

        raptor_mod._tree_cache = {}
        raptor_mod._tree_generation = 0
        raptor_mod._tree_generation_seen = -1  # 模拟进程冷启动初态
        try:
            async with async_session_factory() as db:
                db.add(RaptorCluster(document_id="doc-cold", tree_level=1,
                                     cluster_id="L1C0",
                                     summary_chunk_index=_SUMMARY_INDEX_BASE + 1000,
                                     child_chunk_indices=[0]))
                await db.commit()
                await raptor_mod.refresh_tree_cache(db)
            assert "doc-cold" in raptor_mod._tree_cache
        finally:
            from sqlalchemy import delete

            async with async_session_factory() as db:
                await db.execute(delete(RaptorCluster).where(
                    RaptorCluster.document_id == "doc-cold"))
                await db.commit()
            raptor_mod.invalidate_tree_cache()

    @pytest.mark.asyncio
    async def test_invalidate_during_refresh_discards_result(self, monkeypatch):
        """批次B 补审 P1 配套：查库期间代数变化 → 本轮结果作废不发布"""
        import app.rag.raptor as raptor_mod
        from app.infrastructure.database import async_session_factory
        from app.models.raptor_cluster import RaptorCluster

        raptor_mod._tree_cache = {}
        raptor_mod._tree_generation = 5
        raptor_mod._tree_generation_seen = 4
        # 在查库与发布之间注入 invalidate：包装 db.execute 第一次调用后递增代数
        try:
            async with async_session_factory() as db:
                db.add(RaptorCluster(document_id="doc-race", tree_level=1,
                                     cluster_id="L1C0",
                                     summary_chunk_index=_SUMMARY_INDEX_BASE + 1000,
                                     child_chunk_indices=[0]))
                await db.commit()

                orig_execute = db.execute

                async def race_execute(stmt, *a, **k):
                    result = await orig_execute(stmt, *a, **k)
                    raptor_mod.invalidate_tree_cache()  # 模拟查库期间建树提交
                    return result

                monkeypatch.setattr(db, "execute", race_execute)
                await raptor_mod.refresh_tree_cache(db)
            # 代数变了：本轮作废——seen 仍落后 generation，且缓存未被发布
            assert raptor_mod._tree_generation_seen != raptor_mod._tree_generation
            assert raptor_mod._tree_cache == {}  # 批次B.5 P3-10：钉住"不发布"
        finally:
            from sqlalchemy import delete

            async with async_session_factory() as db:
                await db.execute(delete(RaptorCluster).where(
                    RaptorCluster.document_id == "doc-race"))
                await db.commit()


# ── 清理 ──


class TestDeleteTree:
    @pytest.mark.asyncio
    async def test_delete_removes_cluster_rows(self):
        from app.infrastructure.database import async_session_factory
        from app.models.raptor_cluster import RaptorCluster
        from sqlalchemy import select

        async with async_session_factory() as db:
            db.add_all([
                RaptorCluster(document_id="doc-6", tree_level=1, cluster_id="L1C0",
                              summary_chunk_index=_SUMMARY_INDEX_BASE + 1000,
                              child_chunk_indices=[0, 1]),
                RaptorCluster(document_id="doc-6", tree_level=1, cluster_id="L1C1",
                              summary_chunk_index=_SUMMARY_INDEX_BASE + 1001,
                              child_chunk_indices=[2, 3]),
            ])
            await db.commit()

        removed = await raptor.delete_tree_for_document("doc-6")
        assert removed == 2

        async with async_session_factory() as db:
            rows = (await db.execute(
                select(RaptorCluster).where(RaptorCluster.document_id == "doc-6")
            )).scalars().all()
        assert rows == []
