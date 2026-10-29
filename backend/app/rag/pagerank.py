"""文档引用图 PageRank 重要性信号（验收批次1·1b）

思路：文档 A 的内容提到文档 B 的标题 → A 引用了 B → 建边 A→B；
PageRank 幂迭代出每篇文档的"被引用权威度"。检索时 RRF 融合分
×(1+α·pr) 加成（α=settings.retrieval_pagerank_alpha，0=关闭）。

图节点用 Chroma 元数据的 document_id（UUID）——检索侧融合键 doc_id#chunk_index
可直接匹配；标题 stem 只用于"提到即引用"的匹配与汇编源排除。

挖引用规则（针对本语料现实，避免噪声）：
- 排除"汇编型副本"文档作为引用来源（管理制度汇编/员工手册 的目录与全量
  拼接会把所有文档标题都"提到"，形成全连接图、信号被稀释为常数）——
  与语料生成器引用矩阵同一原则（scripts/gen_demo_docs.py _CITATIONS）：
  副本只该被压低、不该当引用源给别的文档投票；引用矩阵注入端同样承诺
  绝不引用副本（引用即 pagerank 投票，给副本投票抵消权威区分度）。
  两处口径说明：本集合只枚举当前语料中的两份汇编型副本；生成器铁律
  覆盖的四类（手册/汇编/解读/白皮书）中，解读与白皮书当前未被引用、
  故未列入——如未来出现引用它们的语料，需同步补进本集合
- 标题需 ≥4 字（防短词误命中）；不引用自身。
"""
import threading

from app.rag.retriever import get_vector_store

# 缓存：文档块总数未变则复用（与 BM25 缓存同款"计数即失效"策略）
_lock = threading.Lock()
_cache: dict[str, float] | None = None
_cache_chunk_count: int = -1

# 汇编型副本不参与引用投票（见模块 docstring；与生成器引用矩阵同一原则）
_EXCLUDED_SOURCES = {"管理制度汇编", "员工手册"}
_MIN_TITLE_LEN = 4


def _mine_edges(chunks: list[dict]) -> dict[str, set[str]]:
    """扫块内容挖引用：返回 {源文档doc_id: {被引文档doc_id}}"""
    # filename → doc_id（多块同文档取任一即可）
    stem_to_docid: dict[str, str] = {}
    doc_stem: dict[str, str] = {}
    for c in chunks:
        filename = str(c.get("filename") or "")
        doc_id = str(c.get("document_id") or "")
        if not filename or not doc_id:
            continue
        stem = filename.rsplit(".", 1)[0]
        if len(stem) >= _MIN_TITLE_LEN:
            stem_to_docid.setdefault(stem, doc_id)
            doc_stem.setdefault(doc_id, stem)
    stems = list(stem_to_docid.keys())

    edges: dict[str, set[str]] = {}
    for c in chunks:
        doc_id = str(c.get("document_id") or "")
        stem = doc_stem.get(doc_id)
        if not stem or stem in _EXCLUDED_SOURCES:
            continue
        content = str(c.get("content") or "")
        targets = set()
        for other in stems:
            if other != stem and other in content:
                targets.add(stem_to_docid[other])
        if targets:
            edges[doc_id] = edges.get(doc_id, set()) | targets
    return edges


def _power_iterate(edges: dict[str, set[str]], doc_ids: list[str]) -> dict[str, float]:
    """PageRank 幂迭代（阻尼 0.85），归一化到最大值为 1"""
    n = len(doc_ids)
    if n == 0:
        return {}
    d = 0.85
    idx = {s: i for i, s in enumerate(doc_ids)}
    out_deg = {s: len(t) for s, t in edges.items()}
    in_edges: dict[str, list[str]] = {s: [] for s in doc_ids}
    for src, targets in edges.items():
        for t in targets:
            if t in in_edges:
                in_edges[t].append(src)

    pr = [1.0 / n] * n
    for _ in range(30):
        new_pr = [(1 - d) / n] * n
        for t, sources in in_edges.items():
            mass = sum(pr[idx[s]] / out_deg[s] for s in sources)
            new_pr[idx[t]] += d * mass
        pr = new_pr
    top = max(pr) if pr else 1.0
    return {s: p / top for s, p in zip(doc_ids, pr)}


def get_pagerank_map() -> dict[str, float]:
    """返回 {文档document_id: 归一化pagerank}；带缓存（块总数不变则复用）"""
    global _cache, _cache_chunk_count
    store = get_vector_store()
    collection = store._collection
    count = collection.count()
    with _lock:
        if _cache is not None and _cache_chunk_count == count:
            return _cache
    result = collection.get(include=["documents", "metadatas"])
    chunks = []
    for i, doc_id in enumerate(result.get("ids", [])):
        meta = result.get("metadatas")[i] or {}
        chunks.append({
            "content": result.get("documents")[i] or "",
            "filename": meta.get("filename", ""),
            "document_id": meta.get("document_id", ""),
        })
    edges = _mine_edges(chunks)
    doc_ids = sorted({c["document_id"] for c in chunks if c["document_id"]})
    pr = _power_iterate(edges, doc_ids)
    with _lock:
        _cache = pr
        _cache_chunk_count = count
    return pr
