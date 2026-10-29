"""ChromaDB 检索器 + BM25 混合检索"""
import os
import threading
import jieba
# P2-7：Chroma 向量库改用官方独立包 langchain-chroma
# （langchain_community.vectorstores.Chroma 已被官方弃用并计划移除）
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from app.config import settings
from app.rag.embeddings import get_embeddings
from loguru import logger

# ChromaDB 单例
_vector_store: Chroma | None = None
# BM25 检索器缓存。
# 修复 P2-1：命中判断改用「dirty 标记 + 分块总数（count() 走 SQL COUNT，开销极小）」，
# 不再每次先 collection.get(全量) 再比较——旧实现缓存命中也白扫全库。
# 修复 P2-2：检索调用将在线程池执行（asyncio.to_thread），模块级状态加锁保护。
# 修复（质检-阶段4）：缓存记录 top_k——请求的 top_k 大于缓存构建值时重建，
# 否则先用小 top_k 构建、后请求大 top_k 时 BM25 腿只贡献过少的候选。
_bm25_retriever = None
_bm25_chunk_count: int = 0
_bm25_top_k: int = 0
_bm25_dirty: bool = True
_bm25_lock = threading.Lock()


def _invalidate_bm25() -> None:
    """文档集合发生增删后调用：标记 BM25 缓存失效，下次检索时重建"""
    global _bm25_dirty
    with _bm25_lock:
        _bm25_dirty = True


def get_vector_store() -> Chroma:
    """获取 ChromaDB 向量存储实例"""
    global _vector_store
    if _vector_store is None:
        os.makedirs(settings.chroma_persist_dir, exist_ok=True)
        _vector_store = Chroma(
            collection_name="knowledge_chunks",
            embedding_function=get_embeddings(),
            persist_directory=settings.chroma_persist_dir,
        )
    return _vector_store


def get_retriever(
    top_k: int = 6,
    doc_ids: list[str] | None = None,
    category_ids: list[str] | None = None,
    parent_category_ids: list[str] | None = None,
):
    """获取纯向量检索器（MMR），必须传入可见文档范围（doc_ids），
    可选再按分类过滤（category_ids 与 parent_category_ids 互斥）。

    企业知识库（阶段1）：doc_ids 是权限隔离的 fail-closed 依据——
    不传（None）直接报错，防止调用方漏传过滤导致越权读到别的库/部门文档；
    空列表返回永远为空的检索器（用户无可见文档）。

    B15 修复：MMR 的 k/fetch_k 超过实际分块数会抛异常（小知识库/小分类必现），
    这里按「过滤后实际分块数」收敛 k 与 fetch_k；空集合返回永远为空的检索器。
    """
    if doc_ids is None:
        raise ValueError(
            "检索必须传入可见文档范围 doc_ids（fail-closed）："
            "请先通过 kb_service.get_visible_doc_ids 计算用户可见文档"
        )
    store = get_vector_store()
    filter_dict = {"document_id": {"$in": list(doc_ids)}}
    if category_ids:
        filter_dict = {
            "$and": [filter_dict, {"category_id": {"$in": category_ids}}]
        }
    elif parent_category_ids:
        filter_dict = {
            "$and": [filter_dict, {"parent_category_id": {"$in": parent_category_ids}}]
        }

    # 计算过滤后的实际分块数（仅取 ids，不拉文档内容/向量；失败则不做收敛）
    total: int | None = None
    try:
        matched = store._collection.get(where=filter_dict, include=[])
        total = len(matched.get("ids", []))
    except Exception as e:
        logger.debug(f"统计分块数失败，跳过 k/fetch_k 收敛: {e}")
        total = None

    if total is not None and total <= 0:
        # 空集合：返回空的检索器，让调用方走逐级扩搜/无结果提示
        class _EmptyRetriever(BaseRetriever):
            class Config:
                arbitrary_types_allowed = True

            def _get_relevant_documents(self, query, *, run_manager=None) -> list:
                return []

        return _EmptyRetriever()

    if total is not None:
        k = min(top_k, total)
        fetch_k = min(top_k * 3, total)
    else:
        k, fetch_k = top_k, top_k * 3

    search_kwargs: dict = {"k": k, "fetch_k": fetch_k}
    search_kwargs["filter"] = filter_dict
    return store.as_retriever(
        search_type="mmr",  # MMR: 平衡相关性和多样性
        search_kwargs=search_kwargs,
    )


# ── BM25 关键词检索器 ──


class BM25Retriever(BaseRetriever):
    """基于 jieba 分词的 BM25 关键词检索器（验收批次1·8a：自写逐词加权打分）

    旧实现用 rank_bm25.get_scores，不支持逐词权重——同义词展开与词权重
    （含 <1 降权）现在由 expand_terms 给出 [(词, 权重)]，打分侧按
    BM25 标准公式逐词加权求和（k1=1.5、b=0.75；IDF 取
    log((N-df+0.5)/(df+0.5)+1) 的 BM25+ 平滑形式，与 rank_bm25 的
    0.2.2 实现含 epsilon 地板的 IDF 在数值上近似但不完全等同）。
    """

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, documents: list[Document], top_k: int = 4):
        super().__init__()
        import math
        from collections import Counter

        self._docs = documents
        self._top_k = top_k
        # jieba 分词 + 词频预统计（构建时一次，查询时零重复计算）。
        # 验收批次1·1c：索引文本取父块全文（parent_content）——BM25 腿按
        # 完整上下文打分（子块文本太短，词面信号碎片化致排名劣化，实测
        # 模糊题 MRR 0.783→0.611）；向量腿保持子块（语义聚焦）。
        # 返回仍为原文档对象，去重键与向量腿一致。
        self._index_texts = [
            (doc.metadata or {}).get("parent_content") or doc.page_content
            for doc in documents
        ]
        self._tokenized = [list(jieba.cut(text)) for text in self._index_texts]
        self._tf = [Counter(tokens) for tokens in self._tokenized]
        n = max(1, len(self._tokenized))
        self._avgdl = sum(len(t) for t in self._tokenized) / n
        df: dict[str, int] = {}
        for tokens in self._tokenized:
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1
        self._idf = {t: math.log((n - c + 0.5) / (c + 0.5) + 1.0) for t, c in df.items()}
        self._k1 = 1.5
        self._b = 0.75

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
        # 验收批次1（1a/8a）：查询扩展出 [(词, 权重)]——同义词展开解决
        # "问法不同词面漏配"（如 礼品↔礼金），词权重 >1 放大、<1 降权
        from app.rag.retrieval_terms import expand_terms

        query_terms = expand_terms(query)
        scored: list[tuple[float, int]] = []
        for i, tf in enumerate(self._tf):
            dl = len(self._tokenized[i])
            score = 0.0
            for term, weight in query_terms:
                c = tf.get(term, 0)
                if c <= 0:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = c + self._k1 * (1 - self._b + self._b * dl / self._avgdl)
                score += weight * idf * c * (self._k1 + 1) / denom
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._docs[i] for _, i in scored[:self._top_k]]


def _build_bm25_retriever(top_k: int = 4) -> BM25Retriever | None:
    """从 ChromaDB 中加载所有文档构建 BM25 检索器。

    缓存命中路径只做一次 SQL COUNT，不再全量拉取文档与分词。
    所有文档变更都经由 _invalidate_bm25 置脏；构建前消费失效标记，
    构建期间若再次失效则放弃写入缓存（防陈旧数据）。

    P3-3：top_k 参数化（默认 4 保持兼容）——粗召回阶段传入更大候选数，
    与向量路一起供 rerank 精排。缓存命中要求 top_k <= 构建时的 top_k：
    请求更大候选数时重建（小→大方向不丢候选；大→小方向直接复用再截断）。
    """
    global _bm25_retriever, _bm25_chunk_count, _bm25_top_k, _bm25_dirty
    try:
        store = get_vector_store()
        collection = store._collection
        with _bm25_lock:
            if _bm25_retriever is not None and not _bm25_dirty:
                if collection.count() == _bm25_chunk_count and top_k <= _bm25_top_k:
                    return _bm25_retriever
            # 缓存缺失或已失效（或请求更大 top_k）：消费失效标记，开始重建
            _bm25_dirty = False

        result = collection.get(include=["documents", "metadatas"])
        if not result or not result.get("documents"):
            logger.warning("ChromaDB 中无文档，BM25 检索器不可用")
            with _bm25_lock:
                _bm25_dirty = True
            return None

        metadatas = result.get("metadatas") or [{} for _ in result["documents"]]
        # 批次11a：RAPTOR 摘要块不进 BM25 索引——细节题靠 BM25 词面匹配
        # 叶子块（"丧假几天/密码多久换"类词面强匹配题，摘要块的词面命中会
        # 挤占叶子槽位，regression 实测 98.21%→92.86%）；宏观题靠向量腿
        # 语义命中摘要块（+RRF 加成），词面宏观匹配由 doc_summary 目录块承担
        docs = [
            Document(page_content=content, metadata=meta)
            for content, meta in zip(result["documents"], metadatas)
            if (meta or {}).get("chunk_type") != "raptor_summary"
        ]

        retriever = BM25Retriever(docs, top_k=top_k)
        with _bm25_lock:
            if _bm25_dirty:
                # 构建期间有新的文档增删（_invalidate_bm25 被调用）：
                # 本次结果可能已过期，不写入缓存，下次检索会重新构建
                logger.info("BM25 构建期间发生文档变更，放弃写入缓存")
                return retriever
            _bm25_retriever = retriever
            _bm25_chunk_count = len(docs)
            _bm25_top_k = top_k
        logger.info(f"BM25 检索器已构建，共 {len(docs)} 个文档块 (top_k={top_k})")
        return retriever
    except Exception as e:
        logger.warning(f"构建 BM25 检索器失败: {e}")
        with _bm25_lock:
            # 构建失败后强制下次重试：防止陈旧缓存因 count 恰好相同被长期服务
            _bm25_dirty = True
        return None


def notify_decay_factor(age_days: float) -> float:
    """通知/公告时效衰减系数（验收批次1·8i）：90 天内全权重；365 天内 0.7；更旧 0.4。

    时态感知检索：员工 11 月问"国庆放几天"不应把过期通知排第一——
    近期通知优先，过期通知降权（不删除，历史问题仍可查到）。
    """
    if age_days <= 90:
        return 1.0
    if age_days <= 365:
        return 0.7
    return 0.4


def get_hybrid_retriever(
    top_k: int = 6,
    doc_ids: list[str] | None = None,
    category_ids: list[str] | None = None,
    parent_category_ids: list[str] | None = None,
    pagerank_alpha: float = 0.0,
    notify_decay: dict[str, float] | None = None,
):
    """
    混合检索器：融合向量检索（MMR）和 BM25 关键词检索
    使用 Reciprocal Rank Fusion (RRF) 合并结果

    Args:
        top_k: 返回文档数
        doc_ids: 可见文档 ID 列表（必填，权限隔离 fail-closed，见 get_retriever）
        category_ids: 子分类ID列表，用于精确过滤。None 表示不过滤。
        parent_category_ids: 父分类ID列表，用于宽泛过滤。与 category_ids 互斥。
        pagerank_alpha: 验收批次1·1b——文档引用图 PageRank 加成系数，
            RRF 融合分 ×(1+α·pr)；0=关闭（默认）。相关度定大局、重要度只做微调。
        notify_decay: 验收批次1·8i——{文档id: 时效衰减系数}，通知/公告类文档
            按发布时间降权（时态感知）；None=不衰减。
    """
    # 批次11a：两腿候选窗口扩到 max(top_k×2, 40)——语料扩到 306 文档/
    # 646 块后，12 的下限（60 文档时代定）对"中等相关"块太窄：丧假题
    # 实测向量腿 12 窗口漏掉年假块（被摘要块/FAQ 挤占），单腿分进不了
    # top6；行业基准每流召回 50~100、RAGFlow 召回池 1024，40 已收敛。
    # 扩窗后 RRF 融合再截断 top_k，不影响最终返回条数
    recall_k = max(top_k * 2, 40)
    vector_retriever = get_retriever(
        top_k=recall_k, doc_ids=doc_ids,
        category_ids=category_ids, parent_category_ids=parent_category_ids,
    )
    bm25_retriever = _build_bm25_retriever(top_k=recall_k)

    if bm25_retriever is None:
        logger.info("BM25 不可用，回退到纯向量检索")
        return vector_retriever

    class HybridRetriever(BaseRetriever):
        class Config:
            arbitrary_types_allowed = True

        def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
            # 双路召回
            vector_docs = vector_retriever.invoke(query)
            bm25_docs = bm25_retriever.invoke(query)

            # BM25 索引是全库的：内存侧按可见文档 + 分类过滤（向量检索已在 ChromaDB 层过滤）
            doc_id_set = set(doc_ids or [])
            bm25_docs = [
                d for d in bm25_docs
                if d.metadata.get("document_id", "") in doc_id_set
            ]
            if category_ids:
                bm25_docs = [
                    d for d in bm25_docs
                    if d.metadata.get("category_id", "") in category_ids
                ]
            elif parent_category_ids:
                bm25_docs = [
                    d for d in bm25_docs
                    if d.metadata.get("parent_category_id", "") in parent_category_ids
                ]

            # RRF 融合
            # 修复：去重 key 由 page_content[:100] 改为 document_id + chunk_index。
            # （1）前 100 字相同的不同分块不会再被误合并；
            # （2）同一分块在向量/BM25 两路的结果能正确识别为同一条。
            def _doc_key(doc: Document) -> str:
                meta = doc.metadata or {}
                did = str(meta.get("document_id", ""))
                idx = str(meta.get("chunk_index", ""))
                # 批次11a：raptor_summary 摘要块用独立命名空间（chunk_index
                # 虽已偏移 100000+ 编号，但显式区分更清晰——摘要块与叶子块
                # 永不在 RRF 里互相去重合并）
                if meta.get("chunk_type") == "raptor_summary":
                    return f"{did}#raptor-{meta.get('tree_level', '?')}-{meta.get('cluster_id', '?')}"
                if did or idx:
                    return f"{did}#{idx}"
                return doc.page_content  # 无元数据兜底

            k = 60  # RRF 平滑参数
            scores = {}

            # 两路各自从 rank 0 独立计分再求和：若合并成一个 enumerate，
            # BM25 独有文档的 rank 会整体下移 len(vector_docs) 位，
            # 与向量独有文档的相对排序会被无意义地翻转
            for rank, doc in enumerate(vector_docs):
                key = _doc_key(doc)
                scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)

            for rank, doc in enumerate(bm25_docs):
                key = _doc_key(doc)
                scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)

            # 批次B：树遍历腿（第三条）——RAPTOR 树逐层下钻的叶子块，
            # 与向量/BM25 同权参与 RRF；树遍历只返回叶子块（正常 _doc_key），
            # 与两腿天然去重合并。
            # B.5 延迟优化：树腿不再独立调嵌入 API（query 向量在此嵌入后
            # 传入共享，省树腿侧独立调用）；无树缓存时（has_cached_trees
            # False）整体零成本跳过——无厚文档的库不付任何树腿开销
            tree_docs: list[Document] = []
            if settings.raptor_traversal_enabled:
                from app.rag import raptor as raptor_mod

                if raptor_mod.has_cached_trees():
                    try:
                        qv = get_embeddings().embed_query(query)
                        tree_docs = raptor_mod.tree_traversal_search(
                            query, doc_ids or [], query_vector=qv
                        )
                    except Exception as e:
                        # 树腿执行失败：静默降级（不阻塞主链路；留日志可查）
                        logger.debug(f"树腿检索失败（本轮跳过）: {e}")
                        tree_docs = []
                for rank, doc in enumerate(tree_docs):
                    key = _doc_key(doc)
                    scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)

            # 验收批次1·1b：pagerank 重要性加成（相关度定大局、重要度微调决胜）
            if pagerank_alpha > 0:
                from app.rag.pagerank import get_pagerank_map

                pr_map = get_pagerank_map()
                for key in list(scores.keys()):
                    doc_id = key.split("#", 1)[0]
                    pr = pr_map.get(doc_id, 0.0)
                    if pr > 0:
                        scores[key] *= 1 + pagerank_alpha * pr

            # 验收批次1·8i：通知/公告时效衰减（时态感知检索）
            if notify_decay:
                for key in list(scores.keys()):
                    doc_id = key.split("#", 1)[0]
                    factor = notify_decay.get(doc_id, 1.0)
                    if factor != 1.0:
                        scores[key] *= factor

            # 合并去重：同一 key 只保留一个 Document（三腿同源，保留先到者即可）
            merged = {}
            for doc in vector_docs + bm25_docs + tree_docs:
                key = _doc_key(doc)
                if key not in merged:
                    merged[key] = doc

            # 验收批次1·1f：目录摘要块加成——宏观问题（整体/章节/涵盖）的
            # 答案载体；987 字长目录在 BM25 长度归一化下吃亏，固定加成保证可召回
            for key, doc in merged.items():
                if (doc.metadata or {}).get("chunk_type") == "doc_summary":
                    scores[key] = scores.get(key, 0) * 1.5
                # 批次11a：RAPTOR 摘要块同款加成（长摘要块在 BM25 长度
                # 归一化下吃亏；系数 raptor_summary_boost 可配，评估调参）
                elif (doc.metadata or {}).get("chunk_type") == "raptor_summary" \
                        and settings.raptor_summary_boost != 1.0:
                    scores[key] = scores.get(key, 0) * settings.raptor_summary_boost

            # 按 RRF 分数排序，取 top_k
            sorted_docs = sorted(
                merged.values(),
                key=lambda d: scores.get(_doc_key(d), 0),
                reverse=True,
            )
            return sorted_docs[:top_k]

    return HybridRetriever()


def expand_with_chapter_siblings(
    docs: list, max_siblings_per_doc: int = 3, max_total_extra: int = 6
) -> list:
    """TOC 轻量版（验收批次1·1c）：命中块带 section_path（条款/标题层级块）时，
    补同文档同章的其他块作 LLM 上下文——章节级召回，长制度/手册的
    "命中一点、看懂整章"。

    只追加不重排：原排序不动，兄弟块追加在后（引用标注仍指向原命中块）。
    """
    if not docs:
        return docs
    store = get_vector_store()
    collection = store._collection
    out: list[Document] = list(docs)
    seen = {
        (d.metadata.get("document_id", ""), d.metadata.get("chunk_index", -1))
        for d in docs
    }
    extra = 0
    # 批次5 M1：per-call 文档块缓存——同文档多命中块只查一次 Chroma
    # （此前每个命中块都全量取该文档所有块，重复查询）
    doc_chunks_cache: dict[str, dict] = {}
    for d in list(docs):
        meta = d.metadata or {}
        path = meta.get("section_path")
        if not path or extra >= max_total_extra:
            continue
        doc_id = meta.get("document_id", "")
        if not doc_id:
            continue
        # 批次4 修复：Chroma 的 where 只支持单键条件，复合条件
        # {"document_id": X, "section_path": Y} 抛 ValueError
        # "Expected where to have exactly one operator"——
        # 改为单键查询 + Python 侧按 section_path 过滤（文档块量级小）
        if doc_id not in doc_chunks_cache:
            doc_chunks_cache[doc_id] = collection.get(
                where={"document_id": doc_id},
                include=["documents", "metadatas"],
            )
        r = doc_chunks_cache[doc_id]
        per_doc = 0
        for i, content in enumerate(r.get("documents", [])):
            m = dict(r.get("metadatas")[i] or {})
            if m.get("section_path") != path:
                continue
            key = (m.get("document_id", ""), m.get("chunk_index", -1))
            if key in seen:
                continue
            seen.add(key)
            out.append(Document(page_content=content, metadata=m))
            extra += 1
            per_doc += 1
            if per_doc >= max_siblings_per_doc or extra >= max_total_extra:
                break
        if extra >= max_total_extra:
            break
    return out


def add_documents_to_store(documents: list[Document]):
    """添加文档到向量存储，并使 BM25 缓存失效"""
    store = get_vector_store()
    store.add_documents(documents)
    _invalidate_bm25()
    logger.info(f"已添加 {len(documents)} 个文档块，BM25 缓存已失效")


def delete_document_from_store(document_id: str) -> int:
    """
    根据文档ID删除向量数据，并使 BM25 缓存失效。
    返回删除的分块数量，如果删除失败则抛出异常。
    """
    store = get_vector_store()
    collection = store._collection

    # 先查询该文档有多少分块
    existing = collection.get(
        where={"document_id": document_id},
        include=["metadatas"],
    )
    expected_count = len(existing.get("ids", []))

    if expected_count == 0:
        logger.warning(f"文档 {document_id} 在向量库中无分块数据，可能已被清理")
        _invalidate_bm25()
        return 0

    # 执行删除
    result = collection.delete(where={"document_id": document_id})
    deleted_count = result.get("deleted", 0) if isinstance(result, dict) else 0

    # 清除 BM25 缓存
    _invalidate_bm25()

    if deleted_count == 0 and expected_count > 0:
        raise RuntimeError(
            f"向量删除失败！文档 {document_id} 预期删除 {expected_count} 个分块，"
            f"实际删除 0。请检查 ChromaDB 状态。"
        )
    else:
        logger.info(
            f"已删除文档 {document_id} 的 {deleted_count}/{expected_count} 个分块，BM25 缓存已失效"
        )

    return deleted_count


def cleanup_orphan_chunks(valid_document_ids: set[str]) -> int:
    """
    清理 ChromaDB 中无对应 SQL 记录的孤立分块。

    Args:
        valid_document_ids: SQL 数据库中存在的文档 ID 集合

    Returns:
        清理的孤立分块数量
    """
    store = get_vector_store()
    collection = store._collection
    result = collection.get(include=["metadatas"])

    orphan_ids = set()
    for i, meta in enumerate(result.get("metadatas", [])):
        doc_id = meta.get("document_id", "")
        if doc_id and doc_id not in valid_document_ids:
            orphan_ids.add(doc_id)

    total_deleted = 0
    for doc_id in orphan_ids:
        try:
            res = collection.delete(where={"document_id": doc_id})
            count = res.get("deleted", 0) if isinstance(res, dict) else 0
            total_deleted += count
            logger.info(f"清理孤立分块: doc_id={doc_id}, 删除 {count} 个")
        except Exception as e:
            logger.error(f"清理孤立分块失败: doc_id={doc_id}, error={e}")

    if total_deleted > 0:
        _invalidate_bm25()
        logger.info(f"共清理 {total_deleted} 个孤立分块（{len(orphan_ids)} 个文档），BM25 缓存已失效")

    return total_deleted


def get_chunks_by_document(document_id: str) -> list[dict]:
    """根据文档ID获取所有分块（从 ChromaDB）"""
    store = get_vector_store()
    collection = store._collection
    result = collection.get(
        where={"document_id": document_id},
        include=["documents", "metadatas"],
    )
    chunks = []
    if result and result.get("ids"):
        for i, chunk_id in enumerate(result["ids"]):
            meta = result["metadatas"][i] if result.get("metadatas") else {}
            chunks.append({
                "chunk_id": chunk_id,
                "content": result["documents"][i] if result.get("documents") else "",
                "chunk_index": meta.get("chunk_index", i),
                "chunk_hash": meta.get("chunk_hash", ""),
                "filename": meta.get("filename", ""),
                "category_id": meta.get("category_id", ""),
                "parent_category_id": meta.get("parent_category_id", ""),
                "chunk_type": meta.get("chunk_type", ""),
            })
    # 按 chunk_index 排序
    chunks.sort(key=lambda c: c["chunk_index"])
    return chunks


def get_store_stats() -> dict:
    """获取向量存储统计"""
    store = get_vector_store()
    collection = store._collection
    return {
        "total_chunks": collection.count(),
    }


def count_chunks(doc_ids: list[str]) -> int:
    """统计指定文档范围内的分块数（企业知识库阶段1：小库快路的判定依据）。

    判定必须基于"用户可见的分块数"而非全库总数——全库很大、用户只可见
    一小部分时，走全库快路会越权召回别的库文档，也会浪费检索预算。
    """
    if not doc_ids:
        return 0
    store = get_vector_store()
    collection = store._collection
    matched = collection.get(
        where={"document_id": {"$in": list(doc_ids)}}, include=[]
    )
    return len(matched.get("ids", []))
