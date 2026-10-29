"""批次A·⑩ pagerank α 三档对比验证——0（关）/ 0.1（常开小值）/ 0.3（当前默认）

α 作用在 RRF 融合阶段（rerank 之前），两个口径都跑：
- raw（无 rerank）：直接看 α 对检索排序的影响（调参显微镜）
- rerank（生产口径）：看端到端效果（rerank 可能掩盖 α 增量）

用法（backend 目录下）：
    python scripts/eval_pagerank_alpha.py
结果存 eval/pagerank_alpha_snapshots.json，重复跑覆盖同档数据。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

EVAL_DIR = BACKEND_DIR / "eval"
SNAPSHOT_FILE = EVAL_DIR / "pagerank_alpha_snapshots.json"
ALPHAS = [0.0, 0.1, 0.3]
ANSWER_K = 6
RECALL_K = 20


async def _run_layer(alpha: float, queries: list[dict], with_rerank: bool) -> dict:
    from app.rag.chain import retrieve_only
    from app.rag.reranker import rerank_documents
    from app.services.eval_service import load_all_doc_ids

    doc_ids = await load_all_doc_ids()
    from app.services.eval_service import _load_notify_decay

    notify_decay = await _load_notify_decay(doc_ids)
    hit_sum, rr_sum = 0, 0.0
    latencies: list[float] = []
    detail = []
    for item in queries:
        q = item["question"]
        t0 = time.time()
        docs = await asyncio.to_thread(
            retrieve_only, q,
            top_k=RECALL_K if with_rerank else ANSWER_K,
            doc_ids=doc_ids, pagerank_alpha=alpha,
        )
        if with_rerank:
            docs = await asyncio.to_thread(rerank_documents, q, docs, ANSWER_K, notify_decay)
        else:
            docs = docs[:ANSWER_K]
        latencies.append(time.time() - t0)
        names = [d.metadata.get("filename", "") for d in docs]
        hit, rank = False, None
        for i, name in enumerate(names, 1):
            if name in item["expected_docs"]:
                hit, rank = True, i
                break
        hit_sum += 1 if hit else 0
        rr_sum += 1.0 / rank if hit else 0.0
        detail.append({"question": q, "hit": hit, "rank": rank, "top6": names})
    n = len(queries)
    return {
        "n": n,
        "hit_rate": (hit_sum / n) if n else 0.0,
        "mrr": (rr_sum / n) if n else 0.0,
        "avg_ms": (sum(latencies) / n * 1000) if n else 0.0,
        "detail": detail,
    }


def _load_queries(sets: list[str]) -> list[dict]:
    out = []
    for s in sets:
        path = EVAL_DIR / f"queries_{s}.json"
        out.extend(json.loads(path.read_text(encoding="utf-8")))
    return out


async def main() -> None:
    from app.services.eval_service import _load_queries as _lq

    authority_qs = _lq("authority")
    all_qs = _lq("regression") + _lq("long") + _lq("raptor")

    results = []
    for alpha in ALPHAS:
        for rerank_flag, label in ((False, "raw"), (True, "rerank")):
            for layer, qs in (("authority", authority_qs), ("all", all_qs)):
                r = await _run_layer(alpha, qs, rerank_flag)
                results.append({
                    "alpha": alpha, "mode": label, "layer": layer,
                    **{k: v for k, v in r.items() if k != "detail"},
                })
                print(
                    f"α={alpha:<4} [{label:<6}] {layer:<9} "
                    f"hit-rate {r['hit_rate']:.2%} | MRR {r['mrr']:.3f} | "
                    f"{r['avg_ms']:.0f}ms",
                    flush=True,
                )
    SNAPSHOT_FILE.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n快照已存: {SNAPSHOT_FILE.name}")

    # 对比表
    print("\n===== α 三档对比 =====")
    print(f"{'α':<5}{'口径':<8}{'层':<10}{'hit-rate':>10}{'MRR':>9}{'延迟ms':>9}")
    for r in results:
        print(
            f"{r['alpha']:<5}{r['mode']:<8}{r['layer']:<10}"
            f"{r['hit_rate']:>9.2%}{r['mrr']:>9.3f}{r['avg_ms']:>9.0f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
