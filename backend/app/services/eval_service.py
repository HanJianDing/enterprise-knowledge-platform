"""检索评估服务（产品化 B4）：试检索 + 评估集跑分

供两类调用方共用：
- CLI：scripts/eval_retrieval.py（同步包装器，asyncio.run 在主线程安全）
- API：管理端"检索评估"页（async 版本，在请求事件循环内直接 await）

验收修复（A2）：此前 API 端把整个函数丢进 asyncio.to_thread，
函数内部又 asyncio.run 开新事件循环使用 SQLAlchemy 异步引擎——
连接跨循环直接 500。现在 doc_ids 在请求循环内 await 查询，
to_thread 只包纯阻塞的检索/重排调用（不碰数据库）。
"""
import asyncio
import json
import time
from pathlib import Path

from sqlalchemy import select

from app.infrastructure.database import async_session_factory
from app.models.document import Document
from app.rag.chain import retrieve_only
from app.rag.reranker import rerank_documents

ANSWER_K = 6    # 命中判定窗口（与评估基线一致）
RECALL_K = 20   # rerank 模式粗召回
# 验收批次1：评估集分层——回归层（旧能力防退化）+ 语料层（新文档覆盖），
# 后续批次再挂关系层等；分层跑分避免新题稀释旧基线、便于定位退化来源
EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
EVAL_SETS = {
    "regression": EVAL_DIR / "queries_regression.json",
    "long": EVAL_DIR / "queries_long.json",
    "authority": EVAL_DIR / "queries_authority.json",
    "relation": EVAL_DIR / "queries_relation.json",
    "approval": EVAL_DIR / "queries_approval.json",
    "raptor": EVAL_DIR / "queries_raptor.json",
}


def _load_queries(eval_set: str) -> list[dict]:
    """按评估集名加载题目；all=回归层+语料层合并（authority/relation/approval
    为专项层不并入，按需单跑）；未知名回落 regression（防拼写错误误读空集）"""
    if eval_set == "all":
        return _load_queries("regression") + _load_queries("long")
    path = EVAL_SETS.get(eval_set, EVAL_SETS["regression"])
    return json.loads(path.read_text(encoding="utf-8"))


async def _load_notify_decay(doc_ids: list[str]) -> dict[str, float]:
    """批次语料增强：评估侧通知时效衰减（与生产 chat_service 同口径）"""
    from app.services.chat_service import build_notify_decay

    async with async_session_factory() as db:
        return await build_notify_decay(db, doc_ids)


async def load_all_doc_ids() -> list[str]:
    """管理员视角全库文档 ID（async，在调用方事件循环内 await）。
    批次5 7b：superseded 旧版本不参与评估检索（与问答链路同口径）。
    批次B：顺带刷新树遍历缓存（评估检索走树腿需要缓存就绪）"""
    async with async_session_factory() as db:
        from app.rag import raptor as raptor_mod

        await raptor_mod.refresh_tree_cache(db)
        result = await db.execute(
            select(Document.id).where(Document.status != "superseded")
        )
        return [row[0] for row in result.all()]


async def _retrieve_blocking(question: str, top_k: int, doc_ids: list[str]) -> list:
    """纯阻塞检索（嵌入 API/向量库），放线程池执行"""
    return await asyncio.to_thread(
        retrieve_only, question, top_k=top_k, doc_ids=doc_ids
    )


async def retrieval_test_async(
    question: str, top_k: int = 6, with_rerank: bool = False
) -> dict:
    """试检索（async 版）：单问题混合检索（可选 rerank），返回命中切片明细"""
    doc_ids = await load_all_doc_ids()
    if not doc_ids:
        return {"query": question, "top_k": top_k, "reranked": with_rerank, "docs": []}

    docs = await _retrieve_blocking(
        question, RECALL_K if with_rerank else top_k, doc_ids
    )
    if with_rerank:
        docs = await asyncio.to_thread(rerank_documents, question, docs, top_k)
    else:
        docs = docs[:top_k]

    return {
        "query": question,
        "top_k": top_k,
        "reranked": with_rerank,
        "docs": [
            {
                "index": i,
                "filename": d.metadata.get("filename", "未知来源"),
                "chunk_index": d.metadata.get("chunk_index", 0),
                "content": d.page_content,
                "content_preview": d.page_content[:300],
            }
            for i, d in enumerate(docs, 1)
        ],
    }


def _hits(docs: list, expected: list[str]) -> tuple[bool, int | None]:
    """返回 (是否命中任意期望文档, 首个命中位置 1-based；未命中为 None)"""
    names = [d.metadata.get("filename", "") for d in docs]
    for i, name in enumerate(names, 1):
        if name in expected:
            return True, i
    return False, None


async def _run_group_async(queries: list, doc_ids: list[str], with_rerank: bool) -> dict:
    """跑一组题目，返回 {n, hit_rate, mrr, avg_ms, details}"""
    # 批次语料增强：评估口径与生产一致——通知时效衰减进 rerank 层
    notify_decay = await _load_notify_decay(doc_ids)
    detail, hit_sum, rr_sum = [], 0, 0.0
    latencies: list[float] = []
    for item in queries:
        q = item["question"]
        t0 = time.time()
        docs = await _retrieve_blocking(
            q, RECALL_K if with_rerank else ANSWER_K, doc_ids
        )
        docs = (
            await asyncio.to_thread(rerank_documents, q, docs, ANSWER_K,
                                    notify_decay)
            if with_rerank else docs[:ANSWER_K]
        )
        latencies.append(time.time() - t0)
        ok, rank = _hits(docs, item["expected_docs"])
        hit_sum += 1 if ok else 0
        rr_sum += 1.0 / rank if ok else 0.0
        detail.append({
            "question": q,
            "expected": item["expected_docs"],
            "hit": ok,
            "rank": rank,
            "got": [d.metadata.get("filename", "?") for d in docs],
        })

    n = len(queries)
    return {
        "n": n,
        "hit_rate": (hit_sum / n) if n else 0.0,
        "mrr": (rr_sum / n) if n else 0.0,
        "avg_ms": (sum(latencies) / n * 1000) if n else 0.0,
        "details": detail,
    }


async def _run_relation_group_async(queries: list[dict]) -> dict:
    """关系层评估（批次2-2）：走图谱查询链路，按环节打分——
    实体链接准确率 / 路径正确率 / 邻居召回 / 不介入正确率 / 介入率。
    与文档命中口径（hit_rate/mrr）完全不同：关系题验证的是图谱链路本身。"""
    from app.rag.graph import neo4j_store
    from app.rag.graph import query as graph_query

    available = await neo4j_store.is_available()
    details: list[dict] = []
    link_hits = link_n = 0
    path_hits = path_n = 0
    neigh_hits = neigh_n = 0
    noint_hits = noint_n = 0
    comm_hits = comm_n = 0
    intervened = 0

    for item in queries:
        kind = item.get("kind", "path")
        result = await graph_query.search(item["question"]) if available else None
        row: dict = {"question": item["question"], "kind": kind}
        if kind == "no_intervention":
            # 质检 G2：分母=不介入题总数（误介入同样计分母扣分，
            # 否则"全部误介入"反而恒 100% 指标退化）
            noint_n += 1
            if result is None:
                noint_hits += 1
        if result is None:
            row["result"] = None  # 图谱未介入（静默回退纯向量）
            details.append(row)
            continue
        intervened += 1
        row["result"] = {
            "entities": result["entities"],
            "path_steps": result["path"]["steps"] if result["path"] else [],
            "neighbors": [n["name"] for n in result["neighbors"]],
            "community": result.get("community"),
        }
        # 分环节打分（期望值缺失的题不计入对应环节分母，防评估题设计失误误伤）
        expected = set(item.get("expected_entities") or [])
        if expected:
            link_n += 1
            if set(result["entities"]) == expected:
                link_hits += 1
        if kind == "path":
            expected_steps = set(item.get("expected_path") or [])
            if expected_steps:
                path_n += 1
                # 集合相等（步骤文本含方向语义，顺序不比对）
                if set(row["result"]["path_steps"]) == expected_steps:
                    path_hits += 1
        elif kind == "neighbors":
            expected_nb = set(item.get("expected_neighbors") or [])
            if expected_nb:
                neigh_n += 1
                # 子集比对（邻居列表有 limit 截断，允许实际多于期望）
                if expected_nb <= set(row["result"]["neighbors"]):
                    neigh_hits += 1
        elif kind == "community":
            # 批次2-3：社区聚合题——期望成员是实际社区成员的子集
            # （社区成员含部门节点等，期望只写核心人名）
            expected_members = set(item.get("expected_members") or [])
            if expected_members:
                comm_n += 1
                got_members = set(
                    (row["result"]["community"] or {}).get("members", [])
                )
                if expected_members <= got_members:
                    comm_hits += 1
        details.append(row)

    n = len(queries)
    return {
        "n": n,
        "graph_available": available,
        "intervention_rate": (intervened / n) if n else 0.0,
        "entity_link_accuracy": (link_hits / link_n) if link_n else 0.0,
        "path_accuracy": (path_hits / path_n) if path_n else 0.0,
        "neighbor_accuracy": (neigh_hits / neigh_n) if neigh_n else 0.0,
        "no_intervention_accuracy": (noint_hits / noint_n) if noint_n else 0.0,
        "community_accuracy": (comm_hits / comm_n) if comm_n else 0.0,
        "details": details,
    }


async def _run_approval_group_async(queries: list[dict]) -> dict:
    """审批规则层评估（批次2-4 规则腿）：走规则引擎确定性匹配，
    比对期望审批人/动作（与文档命中口径完全不同——规则腿验证的是
    "金额/天数 → 审批人"的确定性映射）。"""
    from app.rag.rules import service as rules_service

    hits = 0
    details: list[dict] = []
    for item in queries:
        result = await rules_service.match_question(item["question"])
        approver_ok = result is not None and result["approver"] == item.get("expected_approver")
        action_ok = result is not None and result["action"] == item.get("expected_action", "审批")
        ok = approver_ok and action_ok
        hits += 1 if ok else 0
        details.append({
            "question": item["question"],
            "hit": ok,
            "expected": f"{item.get('expected_approver')}{item.get('expected_action', '审批')}",
            "got": f"{result['approver']}{result['action']}" if result else None,
        })

    n = len(queries)
    return {
        "n": n,
        "accuracy": (hits / n) if n else 0.0,
        "details": details,
    }


async def run_eval_async(with_rerank: bool = False, eval_set: str = "all") -> dict:
    """评估集跑分（async 版）：标准题与模糊题分组统计。

    eval_set（验收批次1）：all=全量合并 / regression=回归层 / long=语料层
    """
    queries = _load_queries(eval_set)
    # 关系层走图谱专用评估（分环节指标），不复用文档命中口径
    if eval_set == "relation":
        t0 = time.time()
        result = {
            "total_questions": len(queries),
            "doc_count": 0,
            "eval_set": eval_set,
            "with_rerank": with_rerank,
            "relation": await _run_relation_group_async(queries),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
        return result
    # 审批规则层走规则引擎专用评估（确定性匹配准确率）
    if eval_set == "approval":
        t0 = time.time()
        result = {
            "total_questions": len(queries),
            "doc_count": 0,
            "eval_set": eval_set,
            "with_rerank": with_rerank,
            "approval": await _run_approval_group_async(queries),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
        return result
    standard = [q for q in queries if not q.get("fuzzy")]
    fuzzy = [q for q in queries if q.get("fuzzy")]
    doc_ids = await load_all_doc_ids()

    t0 = time.time()
    result = {
        "total_questions": len(queries),
        "doc_count": len(doc_ids),
        "eval_set": eval_set,
        "with_rerank": with_rerank,
        "standard": await _run_group_async(standard, doc_ids, with_rerank),
        "fuzzy": await _run_group_async(fuzzy, doc_ids, with_rerank),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    merged_n = result["standard"]["n"] + result["fuzzy"]["n"]
    merged_hit = (
        result["standard"]["hit_rate"] * result["standard"]["n"]
        + result["fuzzy"]["hit_rate"] * result["fuzzy"]["n"]
    )
    result["total"] = {
        "n": merged_n,
        "hit_rate": merged_hit / merged_n if merged_n else 0.0,
        "mrr": (
            result["standard"]["mrr"] * result["standard"]["n"]
            + result["fuzzy"]["mrr"] * result["fuzzy"]["n"]
        ) / merged_n if merged_n else 0.0,
        "avg_ms": (
            result["standard"]["avg_ms"] * result["standard"]["n"]
            + result["fuzzy"]["avg_ms"] * result["fuzzy"]["n"]
        ) / merged_n if merged_n else 0.0,
    }
    return result


# ── CLI 同步包装器（脚本在主线程调用，asyncio.run 安全；API 请用 async 版） ──

def retrieval_test(question: str, top_k: int = 6, with_rerank: bool = False) -> dict:
    """试检索（同步包装，仅供 CLI/脚本）"""
    return asyncio.run(retrieval_test_async(question, top_k, with_rerank))


def run_eval(with_rerank: bool = False, eval_set: str = "all") -> dict:
    """评估集跑分（同步包装，仅供 CLI/脚本）"""
    return asyncio.run(run_eval_async(with_rerank, eval_set))
