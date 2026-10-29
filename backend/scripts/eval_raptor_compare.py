"""批次11a RAPTOR 量化对比脚本——目录块基线 / RAPTOR 共存 / 纯 RAPTOR 三档对比

跑 long + raptor 两层评估（生产口径：混合检索粗召回 20 → rerank 精排 6），
统计 hit-rate@6 / MRR@6 / 平均延迟 + 特殊块利用率（top6 中
raptor_summary 摘要块 / doc_summary 目录块占比——证明"树真的在点火"）。

用法（在 backend 目录下）：
    # 每次系统状态变化后跑一次，--label 区分档位，结果存快照
    python scripts/eval_raptor_compare.py --label baseline      # 档①：目录块（未建 RAPTOR 树）
    python scripts/eval_raptor_compare.py --label coexist       # 档②：建树后共存
    python scripts/eval_raptor_compare.py --label raptor_only   # 档③：toc_summary_enabled=false + rechunk 后
    # 输出三档对比表
    python scripts/eval_raptor_compare.py --show
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

EVAL_DIR = BACKEND_DIR / "eval"
SNAPSHOT_FILE = EVAL_DIR / "raptor_snapshots.json"
ANSWER_K = 6
RECALL_K = 20


def _load_queries() -> list[dict]:
    """long + raptor 两层合并（厚文档宏观/细节题全集）"""
    long_qs = json.loads((EVAL_DIR / "queries_long.json").read_text(encoding="utf-8"))
    raptor_qs = json.loads((EVAL_DIR / "queries_raptor.json").read_text(encoding="utf-8"))
    return long_qs + raptor_qs


async def _run_questions() -> dict:
    from app.rag.chain import retrieve_only
    from app.rag.reranker import rerank_documents
    from app.services.eval_service import load_all_doc_ids

    doc_ids = await load_all_doc_ids()
    from app.services.eval_service import _load_notify_decay

    notify_decay = await _load_notify_decay(doc_ids)
    queries = _load_queries()
    detail, hit_sum, rr_sum = [], 0, 0.0
    latencies: list[float] = []
    summary_in_top = 0
    toc_in_top = 0
    total_top_slots = 0

    for item in queries:
        q = item["question"]
        t0 = time.time()
        docs = await asyncio.to_thread(
            retrieve_only, q, top_k=RECALL_K, doc_ids=doc_ids
        )
        docs = await asyncio.to_thread(rerank_documents, q, docs, ANSWER_K, notify_decay)
        latencies.append(time.time() - t0)

        names = [d.metadata.get("filename", "") for d in docs]
        types = [d.metadata.get("chunk_type", "") for d in docs]
        hit, rank = False, None
        for i, name in enumerate(names, 1):
            if name in item["expected_docs"]:
                hit, rank = True, i
                break
        hit_sum += 1 if hit else 0
        rr_sum += 1.0 / rank if hit else 0.0
        total_top_slots += len(docs)
        summary_in_top += sum(1 for t in types if t == "raptor_summary")
        toc_in_top += sum(1 for t in types if t == "doc_summary")
        detail.append({
            "question": q,
            "expected": item["expected_docs"],
            "hit": hit,
            "rank": rank,
            "top6": [f"{n}[{t}]" for n, t in zip(names, types)],
        })

    n = len(queries)
    return {
        "n": n,
        "hit_rate": (hit_sum / n) if n else 0.0,
        "mrr": (rr_sum / n) if n else 0.0,
        "avg_ms": (sum(latencies) / n * 1000) if n else 0.0,
        "summary_utilization": (summary_in_top / total_top_slots) if total_top_slots else 0.0,
        "toc_utilization": (toc_in_top / total_top_slots) if total_top_slots else 0.0,
        "detail": detail,
    }


def _load_snapshots() -> list[dict]:
    if not SNAPSHOT_FILE.exists():
        return []
    return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))


def _save_snapshot(label: str, result: dict) -> None:
    snapshots = _load_snapshots()
    snapshots = [s for s in snapshots if s.get("label") != label]  # 同标签覆盖
    snapshots.append({"label": label, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                      **{k: v for k, v in result.items() if k != "detail"}})
    SNAPSHOT_FILE.write_text(
        json.dumps(snapshots, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _print_result(label: str, r: dict, show_details: bool = False) -> None:
    print(f"\n== [{label}] {r['n']} 题 ==")
    print(
        f"  hit-rate@6: {r['hit_rate']:.2%} | MRR@6: {r['mrr']:.3f} | "
        f"平均检索 {r['avg_ms']:.0f}ms"
    )
    print(
        f"  摘要块利用率: {r['summary_utilization']:.2%} | "
        f"目录块利用率: {r['toc_utilization']:.2%}"
    )
    if show_details:
        for d in r["detail"]:
            mark = "✅" if d["hit"] else "❌"
            rank_txt = f"第{d['rank']}位" if d["hit"] else "未命中"
            print(f"  {mark} [{rank_txt}] {d['question']}")
            if not d["hit"]:
                print(f"     期望: {d['expected']}")
                print(f"     实际top6: {d['top6']}")


def _print_compare() -> None:
    snapshots = _load_snapshots()
    if not snapshots:
        print("无快照：先带 --label 跑至少一次")
        return
    order = ["baseline", "coexist", "raptor_only"]
    snapshots.sort(key=lambda s: order.index(s["label"]) if s["label"] in order else 99)
    header = f"{'档位':<14}{'hit-rate@6':>12}{'MRR@6':>10}{'延迟ms':>9}" \
             f"{'摘要块利用':>12}{'目录块利用':>12}"
    print("\n===== RAPTOR 三档对比 =====")
    print(header)
    for s in snapshots:
        print(
            f"{s['label']:<14}{s['hit_rate']:>11.2%}{s['mrr']:>10.3f}"
            f"{s['avg_ms']:>9.0f}{s['summary_utilization']:>11.2%}"
            f"{s['toc_utilization']:>11.2%}  ({s['ts']})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="RAPTOR 摘要树量化对比")
    parser.add_argument("--label", choices=["baseline", "coexist", "raptor_only"],
                        help="本档位标签（结果存快照；同标签覆盖）")
    parser.add_argument("--show", action="store_true", help="输出历史快照对比表")
    parser.add_argument("--details", action="store_true", help="每题明细（与 --label 同用）")
    args = parser.parse_args()

    if args.show:
        _print_compare()
        return

    if not args.label:
        parser.error("需要 --label 或 --show")

    result = asyncio.run(_run_questions())
    _print_result(args.label, result, show_details=args.details)
    _save_snapshot(args.label, result)
    print(f"\n快照已存: {SNAPSHOT_FILE.name}（--show 查看对比表）")


if __name__ == "__main__":
    main()
