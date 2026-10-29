"""RAPTOR 摘要树（批次11a）——UMAP+HDBSCAN 聚类 + LLM 摘要的摘要金字塔

折叠树模式（论文 collapsed-tree）：摘要块与叶子块平铺在同一 Chroma
collection，自然参与向量+BM25 混合检索（RRF 融合，见 retriever.py 加成）；
命中摘要块后按簇关系（raptor_clusters 表）展开子树叶子追加上下文。

建树流程（build_tree_for_document）：
  叶子池（父块语义，sub_chunk 按 parent_id 聚合回父块全文）
  → 嵌入 → UMAP 降维 → HDBSCAN 聚类
  → 每簇 LLM 摘要成上层节点（单块簇直接上浮，不烧 LLM）
  → 递归至单簇或 raptor_max_levels 层
摘要块 metadata：chunk_type=raptor_summary + tree_level + cluster_id；
chunk_index 用 100000+ 偏移独立编号（不与叶子块 enumerate 序列冲突）。

特殊块剔除：doc_summary（目录块）/ sub_chunk（检索子块，聚合回父块）/
raptor_summary（上层节点）。kg/rule 伪块是检索时合成的，不在库中。
"""
import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain_core.documents import Document
from loguru import logger

from app.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# 摘要块 chunk_index 偏移基址：叶子块 chunk_index 是 0..N-1 的 enumerate
# 序列，摘要块从 100000 起编号（层级×1000 + 簇序号），永不相交
_SUMMARY_INDEX_BASE = 100000
_LEVEL_STRIDE = 1000

# 参与建树的叶子块类型（其余为特殊块，剔除）
_CLUSTERABLE_TYPES = {"laws", "long_text", "default", "faq", "table", "structured"}
_SPECIAL_TYPES = {"doc_summary", "sub_chunk", "raptor_summary"}

# 叶子块数下限：少于该数聚类无意义，跳过建树
_MIN_LEAF_CHUNKS = 8


@dataclass
class _Node:
    """树节点：content 为文本（叶子=父块全文；上层=摘要），
    chunk_index 为 Chroma 内的定位索引（叶子=真实块；摘要=偏移编号）"""

    content: str
    chunk_index: int


def _fetch_leaf_pool(doc_id: str) -> list[_Node]:
    """从 Chroma 拉文档块并聚合出叶子池（父块语义）：
    - sub_chunk 按 parent_id 聚合回父块（取 metadata.parent_content 全文，
      块索引取组内最小 chunk_index——展开时命中组首子块即可取回父块）
    - 剔除 doc_summary / raptor_summary 特殊块
    - 其余类型块原样进池
    """
    from app.rag.retriever import get_vector_store

    collection = get_vector_store()._collection
    r = collection.get(
        where={"document_id": doc_id},
        include=["documents", "metadatas"],
    )
    docs = r.get("documents") or []
    metas = r.get("metadatas") or [{} for _ in docs]

    pool: list[_Node] = []
    parent_groups: dict[str, list[tuple[str, int]]] = {}  # parent_id → [(全文, idx)]
    parent_order: list[str] = []
    for content, meta in zip(docs, metas):
        ctype = meta.get("chunk_type", "")
        if ctype in ("doc_summary", "raptor_summary"):
            continue
        idx = meta.get("chunk_index", -1)
        if ctype == "sub_chunk":
            pid = meta.get("parent_id", "") or f"anon-{idx}"
            if pid not in parent_groups:
                parent_groups[pid] = []
                parent_order.append(pid)
            parent_groups[pid].append((meta.get("parent_content") or content or "", idx))
            continue
        pool.append(_Node(content=content or "", chunk_index=idx))

    for pid in parent_order:
        group = parent_groups[pid]
        full = max((t[0] for t in group), key=len) or ""
        if not full:
            continue
        first_idx = min(t[1] for t in group)
        pool.append(_Node(content=full, chunk_index=first_idx))

    pool.sort(key=lambda n: n.chunk_index)  # 确定性：簇内顺序稳定
    return pool


def cluster_chunks(embeddings: list[list[float]]) -> list[list[int]]:
    """UMAP 降维 + HDBSCAN 聚类，返回簇列表（每簇=成员在 embeddings 中的下标）。

    噪声点（HDBSCAN 标 -1）按 RAPTOR 原文做法合成"杂项簇"——噪声块
    语义各异但都是该文档内容，聚在一起摘要不丢信息；
    全噪声/单簇时返回单簇（该层到此为止，见调用方层循环）。
    """
    import numpy as np

    n = len(embeddings)
    if n < 3:
        return [list(range(n))]
    X = np.asarray(embeddings, dtype=np.float32)

    # UMAP：小块数时 n_neighbors/n_components 按 N 自适应收敛。
    # n < 8 时 n_components=2——小图的谱初始化（eigsh）在 k 接近分量点数时
    # 会抛 "k >= N" 退化（实测层 2 节点少时出现），二维足以支撑聚类
    n_neighbors = min(15, n - 1)
    n_components = 2 if n < 8 else min(5, n - 1)
    try:
        import umap

        reducer = umap.UMAP(
            n_neighbors=n_neighbors,
            n_components=n_components,
            metric="cosine",
            random_state=42,
        )
        reduced = reducer.fit_transform(X)
    except Exception as e:
        logger.warning(f"UMAP 降维失败，退化为单簇: {e}")
        return [list(range(n))]

    try:
        import hdbscan

        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=2,
            min_samples=1,
            metric="euclidean",
            cluster_selection_method="eom",
        )
        labels = clusterer.fit_predict(reduced)
    except Exception as e:
        logger.warning(f"HDBSCAN 聚类失败，退化为单簇: {e}")
        return [list(range(n))]

    clusters: dict[int, list[int]] = {}
    noise: list[int] = []
    for i, label in enumerate(labels):
        if label < 0:
            noise.append(i)
        else:
            clusters.setdefault(int(label), []).append(i)
    out = list(clusters.values())
    if noise:
        out.append(noise)  # 杂项簇（RAPTOR 原文做法）
    if len(out) <= 1:
        return [list(range(n))]
    return out


def _summary_prompt(doc_name: str, level: int, texts: list[str]) -> str:
    """簇摘要 prompt：共同主题 + 关键要点，保留数字/名称/条款号；
    首句点明主题——宏观问题的向量/BM25 检索靠它命中"""
    joined = "\n\n".join(f"[块{i + 1}]\n{t[:2000]}" for i, t in enumerate(texts))
    return (
        f"你是企业知识库文档摘要员。以下是《{doc_name}》第 {level} 层"
        f"同一主题的 {len(texts)} 个内容块：\n\n{joined}\n\n"
        f"请输出一段 150~300 字的中文摘要：概括这些块的共同主题与关键要点；"
        f"保留具体数字、人名、条款编号、制度名称；首句点明主题领域"
        f"（例如「本部分规定休假管理相关的总体要求」）。只输出摘要正文，不要任何前缀说明。"
    )


async def _summarize_cluster(doc_name: str, level: int, texts: list[str]) -> str | None:
    """LLM 摘要（temperature=0，超时兜底）；失败返回 None（簇内容不丢——成员已在树上）"""
    from app.rag.llm import get_llm, invoke_with_timeout

    llm = get_llm(streaming=False, temperature=0)
    resp = await invoke_with_timeout(llm, _summary_prompt(doc_name, level, texts))
    if resp is None:
        return None
    text = resp.content if hasattr(resp, "content") else str(resp)
    return (text or "").strip()[:400] or None


async def build_tree_for_document(doc_id: str, filename: str) -> dict | None:
    """为厚文档建摘要树：叶子池 → 逐层聚类+摘要 → 摘要块入 Chroma + 簇关系入表。

    返回统计 {levels, clusters, summaries, llm_calls}；未建（开关关/不够厚/
    叶子少）返回 None；异常只留日志（降级：文档正常可用，见 document_service
    挂钩处 fire-and-forget）。幂等：建前清掉该文档旧摘要块与旧簇行。
    """
    try:
        if not settings.raptor_enabled:
            return None
        pool = await asyncio.to_thread(_fetch_leaf_pool, doc_id)
        total_chars = sum(len(n.content) for n in pool)
        if total_chars < settings.raptor_min_chars or len(pool) < _MIN_LEAF_CHUNKS:
            logger.info(
                f"RAPTOR 跳过 {filename}（{total_chars} 字 / {len(pool)} 叶子块，未达阈值）"
            )
            return None

        from app.rag.embeddings import get_embeddings

        embeddings_fn = get_embeddings()

        summary_docs: list[Document] = []
        cluster_rows: list[dict] = []
        llm_calls = 0
        level = 1
        current = list(pool)
        built_levels = 0

        while len(current) > 1 and level <= settings.raptor_max_levels:
            texts = [n.content for n in current]
            embeddings = await asyncio.to_thread(embeddings_fn.embed_documents, texts)
            clusters = await asyncio.to_thread(cluster_chunks, embeddings)
            if len(clusters) <= 1:
                break
            next_level: list[_Node] = []
            for ci, member_idxs in enumerate(clusters):
                members = [current[i] for i in member_idxs]
                if len(members) == 1:
                    # 单块簇直接上浮（RAPTOR 原文做法，不烧 LLM）
                    next_level.append(members[0])
                    continue
                summary = await _summarize_cluster(filename, level, [m.content for m in members])
                llm_calls += 1
                cluster_id = f"L{level}C{ci}"
                chunk_index = _SUMMARY_INDEX_BASE + level * _LEVEL_STRIDE + ci
                if summary:
                    next_level.append(_Node(content=summary, chunk_index=chunk_index))
                else:
                    # 摘要失败：簇成员直接上浮（不丢内容）
                    next_level.extend(members)
                    continue
                summary_docs.append(Document(
                    page_content=f"《{filename}》第 {level} 层内容摘要：{summary}",
                    metadata={
                        "document_id": doc_id,
                        "filename": filename,
                        "chunk_type": "raptor_summary",
                        "tree_level": level,
                        "cluster_id": cluster_id,
                        "chunk_index": chunk_index,
                    },
                ))
                cluster_rows.append({
                    "document_id": doc_id,
                    "tree_level": level,
                    "cluster_id": cluster_id,
                    "summary_chunk_index": chunk_index,
                    "child_chunk_indices": [m.chunk_index for m in members],
                })
            current = next_level
            built_levels += 1
            level += 1

        if not cluster_rows:
            logger.info(f"RAPTOR {filename} 未形成有效簇（叶子 {len(pool)} 块），跳过")
            return None

        # 幂等清理：旧摘要块 + 旧簇行（reprocess --force 重建路径）
        await _clear_existing(doc_id)

        from app.rag.retriever import add_documents_to_store

        await asyncio.to_thread(add_documents_to_store, summary_docs)

        from app.infrastructure.database import async_session_factory
        from app.models.raptor_cluster import RaptorCluster

        async with async_session_factory() as db:
            db.add_all([RaptorCluster(**row) for row in cluster_rows])
            await db.commit()

        logger.info(
            f"RAPTOR 摘要树建成: {filename} → {len(pool)} 叶子块 / "
            f"{built_levels} 层 / {len(cluster_rows)} 簇 / {llm_calls} 次 LLM 调用"
        )
        invalidate_tree_cache()  # 批次B：树结构变了，树遍历缓存置脏
        return {
            "leaf_chunks": len(pool),
            "levels": built_levels,
            "clusters": len(cluster_rows),
            "summaries": len(summary_docs),
            "llm_calls": llm_calls,
        }
    except Exception as e:
        logger.warning(f"RAPTOR 建树失败（文档 {filename}）: {e}")
        return None


async def _clear_existing(doc_id: str) -> None:
    """清掉该文档旧摘要块（Chroma）与旧簇行（SQL）——重建前幂等清理。
    Chroma where 只支持单键条件（批次4 教训）：按 document_id 拉出后
    Python 侧过滤 chunk_type，再按 ids 删除。"""
    from app.rag.retriever import get_vector_store

    collection = get_vector_store()._collection
    r = collection.get(where={"document_id": doc_id}, include=["metadatas"])
    ids = r.get("ids") or []
    metas = r.get("metadatas") or [{} for _ in ids]
    stale_ids = [
        cid for cid, meta in zip(ids, metas)
        if meta.get("chunk_type") == "raptor_summary"
    ]
    if stale_ids:
        collection.delete(ids=stale_ids)

    from app.infrastructure.database import async_session_factory
    from sqlalchemy import delete
    from app.models.raptor_cluster import RaptorCluster

    async with async_session_factory() as db:
        await db.execute(
            delete(RaptorCluster).where(RaptorCluster.document_id == doc_id)
        )
        await db.commit()


async def expand_summary_children(
    db,
    docs: list,
    max_children_per_summary: int = 3,
    max_total_extra: int = 6,
) -> list:
    """折叠树展开：命中摘要块（raptor_summary）时补其子树叶子作 LLM 上下文。

    递归沿簇表下钻（层 2 摘要 → 层 1 摘要 → 叶子），每摘要块叶子限
    max_children_per_summary、总 max_total_extra（同兄弟块模式）；
    只追加不重排（引用标注仍指向原命中块）。叶子为 sub_chunk 时取
    parent_content 全文（与建树叶子池同口径）。
    """
    if not docs:
        return docs
    summary_hits = [
        d for d in docs
        if (d.metadata or {}).get("chunk_type") == "raptor_summary"
    ]
    if not summary_hits:
        return docs

    from sqlalchemy import select
    from app.models.raptor_cluster import RaptorCluster

    # 文档簇关系缓存：doc_id → {summary_chunk_index: child_chunk_indices}
    doc_maps: dict[str, dict[int, list[int]]] = {}
    wanted: dict[str, set[int]] = {}  # document_id → 叶子 chunk_index 集合

    async def _child_map(doc_id: str) -> dict[int, list[int]]:
        if doc_id not in doc_maps:
            rows = (await db.execute(
                select(RaptorCluster).where(RaptorCluster.document_id == doc_id)
            )).scalars().all()
            doc_maps[doc_id] = {
                r.summary_chunk_index: list(r.child_chunk_indices or []) for r in rows
            }
        return doc_maps[doc_id]

    for hit in summary_hits:
        meta = hit.metadata or {}
        doc_id = meta.get("document_id", "")
        if not doc_id:
            continue
        child_map = await _child_map(doc_id)
        # 递归下钻：命中摘要块 → 逐层解析成员到叶子
        stack = [meta.get("chunk_index", -1)]
        leaf_idxs: list[int] = []
        while stack and len(leaf_idxs) < max_children_per_summary:
            idx = stack.pop(0)
            children = child_map.get(idx)
            if children is None:
                # 无簇记录的索引视为叶子（单块上浮节点的 chunk_index 即叶子索引）
                if idx < _SUMMARY_INDEX_BASE:
                    leaf_idxs.append(idx)
                continue
            for child in children:
                if child >= _SUMMARY_INDEX_BASE:
                    stack.append(child)
                elif len(leaf_idxs) < max_children_per_summary:
                    leaf_idxs.append(child)
        if leaf_idxs:
            wanted.setdefault(doc_id, set()).update(leaf_idxs)
        if sum(len(v) for v in wanted.values()) >= max_total_extra:
            break

    if not wanted:
        return docs

    # 从 Chroma 拉叶子（sub_chunk 取父块全文、parent_id 去重）
    from app.rag.retriever import get_vector_store

    collection = get_vector_store()._collection
    extra_docs: list[Document] = []
    seen_keys = {
        (d.metadata.get("document_id", ""), d.metadata.get("chunk_index", -1))
        for d in docs
    }
    seen_parents: set[str] = set()
    for doc_id, idxs in wanted.items():
        r = collection.get(
            where={"document_id": doc_id},
            include=["documents", "metadatas"],
        )
        idx_set = set(idxs)
        for content, meta in zip(r.get("documents") or [], r.get("metadatas") or [{}]):
            idx = meta.get("chunk_index", -1)
            if idx not in idx_set:
                continue
            if meta.get("chunk_type") == "sub_chunk":
                pid = meta.get("parent_id", "")
                if pid in seen_parents:
                    continue
                seen_parents.add(pid)
                content = meta.get("parent_content") or content
            key = (doc_id, idx)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            extra_docs.append(Document(page_content=content, metadata=dict(meta)))
            if len(extra_docs) >= max_total_extra:
                break
        if len(extra_docs) >= max_total_extra:
            break
    return docs + extra_docs


async def delete_tree_for_document(doc_id: str) -> int:
    """文档删除 → 清理摘要树簇行（Chroma 摘要块由 delete_document_from_store
    按 document_id 天然覆盖）。全程 try/except，失败只留日志。"""
    try:
        from sqlalchemy import delete
        from app.infrastructure.database import async_session_factory
        from app.models.raptor_cluster import RaptorCluster

        async with async_session_factory() as db:
            result = await db.execute(
                delete(RaptorCluster).where(RaptorCluster.document_id == doc_id)
            )
            await db.commit()
            invalidate_tree_cache()
            return result.rowcount or 0
    except Exception as e:
        logger.warning(f"RAPTOR 簇表清理失败（文档 {doc_id}）: {e}")
        return 0


# ── 树遍历检索（批次B：RAPTOR 论文第二种模式）──

# 树结构缓存（doc_id → {"max_level", "clusters": [...]}）——树遍历检索
# 在混合检索的同步路径（to_thread）内执行，不能 await DB；树数据量小
# （厚文档级），由 refresh_tree_cache 全量构建、建树/删树后置脏重建。
# 批次B 质检 P2-1：失效标记用递增代数计数器而非布尔——refresh 的
# "dirty 检查 → await 查库 → 发布 → dirty=False" 非原子，建树提交恰
# 落在 await 期间时置脏会被随后的 dirty=False 吞掉（丢失失效竞态）；
# 代数方案：invalidate 递增代数，refresh 发布前比对，代数变了说明
# 查库期间又有变更 → 本轮结果作废、下一轮重查
_tree_cache: dict[str, dict] = {}
_tree_generation: int = 0          # 当前已发布缓存的代数
_tree_generation_seen: int = -1    # 最近一次**成功发布**快照对应的代数
# （作废轮不更新）。批次B 补审 P1 修复：初值必须 ≠ generation——否则
# 启动时早退条件（seen == generation 且 _tree_cache 非 None）立即成立，
# 首次 refresh 空转、树遍历腿每次重启后静默失效直到下一次建树/删树。
# 批次B.5 P3-1：并发惊群锁——同一新代数下并发首请求各自全量构建只浪费
# 不破坏，加锁让同代并发串行（第二个进入时已命中早退，零重复构建）
_refresh_lock = asyncio.Lock()


def invalidate_tree_cache() -> None:
    """建树/删树后调用：递增代数，标记缓存失效（丢失失效竞态修复）"""
    global _tree_generation
    _tree_generation += 1


async def refresh_tree_cache(db: "AsyncSession") -> None:
    """重建树结构缓存（检索前调用一次；代数未变则命中零成本）。

    批次B.5 P3-1：加惊群锁——同代数并发首请求时第二个进入已命中早退，
    不重复全量构建（锁内查库是异步等待，不阻塞事件循环）"""
    global _tree_cache, _tree_generation_seen
    async with _refresh_lock:
        if _tree_generation_seen == _tree_generation and _tree_cache is not None:
            return
        try:
            from sqlalchemy import select
            from app.models.raptor_cluster import RaptorCluster

            # 查库前记代数；发布前比对——期间有变更则作废（下轮重查）
            gen_before = _tree_generation
            rows = (await db.execute(select(RaptorCluster))).scalars().all()
            cache: dict[str, dict] = {}
            for r in rows:
                entry = cache.setdefault(r.document_id,
                                         {"max_level": 0, "clusters": []})
                entry["max_level"] = max(entry["max_level"], r.tree_level)
                entry["clusters"].append({
                    "level": r.tree_level,
                    "cluster_id": r.cluster_id,
                    "summary_chunk_index": r.summary_chunk_index,
                    "child_chunk_indices": list(r.child_chunk_indices or []),
                })
            if _tree_generation != gen_before:
                # 查库期间又有建树/删树：本轮结果可能过期，不发布（下轮重查）
                return
            _tree_cache = cache
            _tree_generation_seen = gen_before
        except Exception as e:
            logger.warning(f"RAPTOR 树缓存构建失败（树遍历腿本轮跳过）: {e}")


def has_cached_trees() -> bool:
    """树结构缓存是否非空（调用方判断是否值得为树腿做嵌入/遍历——
    B.5 延迟优化：无树时零成本跳过）"""
    return bool(_tree_cache)


def tree_traversal_search(
    query: str,
    doc_ids: list[str],
    top_clusters_per_level: int = 2,
    max_leaves: int = 6,
    max_leaves_per_doc: int = 2,
    query_vector=None,
) -> list[Document]:
    """树遍历检索（同步，在混合检索的 to_thread 路径内调用）：

    对每个有树的可见文档，从树的顶层开始逐层选与 query 余弦相似度最高
    的 top_clusters_per_level 个簇下钻，最终收集路径上的叶子块——宏观
    问题沿"最相关分支"直达答案叶子，层级结构显式参与检索决策
    （折叠树是全池平铺，树遍历是逐层下钻，两种模式互补）。

    缓存未就绪/无树/嵌入失败均返回空列表（静默降级，树腿不阻塞主链路）。
    """
    import numpy as np

    if not settings.raptor_traversal_enabled or not _tree_cache:
        return []
    doc_set = set(doc_ids or [])
    if not doc_set:
        return []

    # B.5 延迟优化：优先复用调用方传入的 query 向量——混合检索侧嵌入后
    # 共享给树腿，树腿不再独立调嵌入 API（省树腿侧 ~0.8s/问的独立调用；
    # 向量腿自身的嵌入仍独立发生，未做注入改造）；未传时自行嵌入
    # （独立调用场景，如冒烟脚本）
    if query_vector is not None:
        qv = np.asarray(query_vector, dtype=np.float32)
    else:
        try:
            from app.rag.embeddings import get_embeddings

            qv = np.asarray(get_embeddings().embed_query(query), dtype=np.float32)
        except Exception as e:
            logger.warning(f"树遍历嵌入失败（本轮跳过）: {e}")
            return []

    from app.rag.retriever import get_vector_store

    collection = get_vector_store()._collection
    # 候选叶子（先全收集，叶子层再做相似度精排——簇级相似度区分度低，
    # 多簇候选正确，但先到先得会让 chunk_index 小的叶子抢占配额）
    leaf_candidates: dict[str, list[tuple]] = {}  # doc_id → [(sim, idx, content, meta)]

    for doc_id in doc_set:
        tree = _tree_cache.get(doc_id)
        if not tree:
            continue
        # 该文档全部块的向量与 metadata（一次拉取）
        try:
            r = collection.get(
                where={"document_id": doc_id},
                include=["documents", "metadatas", "embeddings"],
            )
        except Exception:
            continue
        # 注意：embeddings 是 numpy 数组列表，不能用 `or []`（数组真值检查抛错）
        vecs = r.get("embeddings")
        if vecs is None:
            vecs = []
        metas = r.get("metadatas") or [{} for _ in vecs]
        contents = r.get("documents") or ["" for _ in vecs]
        summaries: dict[int, tuple] = {}  # chunk_index → (vector, meta)
        leaf_blocks: dict[int, tuple] = {}  # chunk_index → (vector, content, meta)
        for vec, meta, content in zip(vecs, metas, contents):
            ctype = meta.get("chunk_type", "")
            idx = meta.get("chunk_index", -1)
            if ctype == "raptor_summary" and vec is not None:
                summaries[idx] = (np.asarray(vec, dtype=np.float32), meta)
            elif ctype not in ("doc_summary",) and vec is not None:
                leaf_blocks[idx] = (np.asarray(vec, dtype=np.float32),
                                    content, meta)
        if not summaries or not leaf_blocks:
            continue
        clusters_by_chunk = {
            c["summary_chunk_index"]: c for c in tree["clusters"]
        }
        # 从顶层开始下钻
        current = [
            c["summary_chunk_index"] for c in tree["clusters"]
            if c["level"] == tree["max_level"]
        ]
        for _ in range(settings.raptor_max_levels + 1):
            if not current:
                break
            scored = []
            for idx in current:
                entry = summaries.get(idx)
                if entry is None:
                    continue
                v = entry[0]
                denom = float(np.linalg.norm(qv)) * float(np.linalg.norm(v))
                if denom <= 0:
                    continue
                sim = float(np.dot(qv, v) / denom)
                scored.append((sim, idx))
            scored.sort(key=lambda t: -t[0])
            chosen = [idx for _, idx in scored[:top_clusters_per_level]]
            if not chosen:
                break
            next_candidates: list[int] = []
            for idx in chosen:
                cluster = clusters_by_chunk.get(idx)
                if cluster is None:
                    continue
                for child in cluster["child_chunk_indices"]:
                    if child >= _SUMMARY_INDEX_BASE:
                        next_candidates.append(child)  # 下层摘要块继续下钻
                    else:
                        block = leaf_blocks.get(child)
                        if block is not None:
                            v = block[0]
                            denom = float(np.linalg.norm(qv)) * float(np.linalg.norm(v))
                            if denom <= 0:
                                continue
                            sim = float(np.dot(qv, v) / denom)
                            leaf_candidates.setdefault(doc_id, []).append(
                                (sim, child, block[1], block[2])
                            )
            current = next_candidates

    # 叶子层精排：每文档按叶子相似度降序取 max_leaves_per_doc，
    # 再全局按相似度合并截断 max_leaves（相似度优先，不再是先到先得）
    picked: list[tuple[float, str, Document]] = []
    for doc_id, cands in leaf_candidates.items():
        cands.sort(key=lambda t: -t[0])
        for sim, idx, content, meta in cands[:max_leaves_per_doc]:
            picked.append((sim, doc_id, Document(
                page_content=content, metadata=dict(meta)
            )))
    picked.sort(key=lambda t: -t[0])
    return [doc for _, _, doc in picked[:max_leaves]]
