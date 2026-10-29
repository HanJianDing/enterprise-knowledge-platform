"""检索质量评估脚本 — 命中率(hit-rate@6) / MRR@6 基线

产品化 B4：核心逻辑已迁移到 app/services/eval_service.py（与"检索评估"管理页
共用同一实现），本脚本只负责 CLI 输出格式。

批次11a 双口径（默认对调）：
- 默认 = rerank 口径（生产真实行为：粗召回 20 → rerank 精排 6）——每批次
  验收/回归的标准口径
- --raw = 纯检索基线（混合检索直截 6，无 rerank）——调 RRF 融合参数时用
  （窗口/加成/pagerank 等检索器内部参数；该口径对检索器变化敏感，
  rerank 不掩盖增量）

用法（在 backend 目录下，需要 .env 中的 DASHSCOPE_API_KEY 与网络）：
    python scripts/eval_retrieval.py        # rerank 口径（生产行为）
    python scripts/eval_retrieval.py --raw  # 纯检索基线（RRF 排序本身）

输出每条问题的命中详情与总体 hit-rate@6 / MRR@6，并附带每问平均检索耗时（延迟基线）。
"""
import argparse
import sys
from pathlib import Path

# 让脚本能 import app.*（backend 目录加入 sys.path）
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.eval_service import run_eval  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 检索质量评估")
    # 批次11a：默认口径改为 rerank（生产行为）；--raw 保留纯检索基线
    # （调 RRF 融合参数时用）。--with-rerank 保留为兼容别名
    parser.add_argument("--raw", action="store_true",
                        help="纯检索基线（无 rerank；调 RRF 融合参数时用）")
    parser.add_argument("--with-rerank", action="store_true",
                        help="[兼容别名] 与默认行为相同（rerank 口径）")
    parser.add_argument("--set", dest="eval_set", choices=["all", "regression", "long", "authority", "relation", "approval", "raptor"],
                        default="all", help="评估集分层：all/regression/long/authority/relation/approval/raptor")
    args = parser.parse_args()

    with_rerank = not args.raw
    result = run_eval(with_rerank=with_rerank, eval_set=args.eval_set)
    mode = "粗召回 20 → rerank 精排 6" if with_rerank else "混合检索 top6（纯检索基线）"
    print(f"评估集: 共 {result['total_questions']} 题（{result['eval_set']} 层）")
    print(f"模式: {mode}")
    if args.eval_set == "relation":
        _print_relation_result(result)
        return
    if args.eval_set == "approval":
        _print_approval_result(result)
        return
    print(f"可见文档范围: {result['doc_count']} 篇文档（全库）")

    for label, key in (("标准检索题", "standard"), ("模糊检索题（跨文档）", "fuzzy")):
        group = result[key]
        print(f"\n== {label} ({group['n']} 题) ==")
        for d in group["details"]:
            mark = "✅" if d["hit"] else "❌"
            rank_txt = f"第{d['rank']}位" if d["hit"] else "未命中"
            print(f"{mark} [{rank_txt}] {d['question']}")
            if not d["hit"]:
                print(f"   期望: {d['expected']}")
                print(f"   实际top6: {d['got']}")
        print(
            f"  hit-rate@6: {group['hit_rate']:.2%} | "
            f"MRR@6: {group['mrr']:.3f} | 单问平均检索 {group['avg_ms']:.0f}ms"
        )
    print(f"\n总耗时: {result['elapsed_ms'] / 1000:.1f}s")


def _print_relation_result(result: dict) -> None:
    """关系层评估输出（批次2-2）：图谱链路分环节指标"""
    r = result["relation"]
    print(f"图谱可用: {'是' if r['graph_available'] else '否（未跑通，全题未介入）'}")
    print(
        f"介入率: {r['intervention_rate']:.2%} | "
        f"实体链接准确率: {r['entity_link_accuracy']:.2%} | "
        f"路径正确率: {r['path_accuracy']:.2%} | "
        f"邻居召回: {r['neighbor_accuracy']:.2%} | "
        f"不介入正确率: {r['no_intervention_accuracy']:.2%}"
    )
    for d in r["details"]:
        if d["result"] is None:
            mark = "✅" if d["kind"] == "no_intervention" else "❌"
            print(f"{mark} [未介入] {d['question']}")
        else:
            ents = "、".join(d["result"]["entities"]) or "（无实体）"
            steps = "；".join(d["result"]["path_steps"])
            nbs = "、".join(d["result"]["neighbors"])
            print(f"   [{d['kind']}] {d['question']} → 实体: {ents}")
            if steps:
                print(f"       路径: {steps}")
            if nbs:
                print(f"       邻居: {nbs}")
    print(f"\n总耗时: {result['elapsed_ms'] / 1000:.1f}s")


def _print_approval_result(result: dict) -> None:
    """审批规则层评估输出（批次2-4 规则腿）：确定性匹配准确率"""
    a = result["approval"]
    print(f"规则引擎匹配准确率: {a['accuracy']:.2%}（{a['n']} 题）")
    for d in a["details"]:
        mark = "✅" if d["hit"] else "❌"
        print(f"{mark} {d['question']} → 期望[{d['expected']}] 实际[{d['got']}]")
    print(f"\n总耗时: {result['elapsed_ms'] / 1000:.1f}s")


if __name__ == "__main__":
    main()
