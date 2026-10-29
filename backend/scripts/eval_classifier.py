"""分类员（意图澄清）评估脚本 — 口语改写质量

口语改写题：把口语化/模糊的问题丢给分类 Agent，检验两件事——
1. 改写质量：clarified_question 是否包含期望关键词（每个关键词组至少命中一个）
2. 分类准确：返回的 category_name 是否与期望一致（期望 null 的不参与统计）

注意：评估分类树是 classifier_queries.json 中的固定快照（保证跨次运行可复现），
与生产环境数据库中的分类树相互独立；生产分类调整后如需同步，请更新该文件。

用法（在 backend 目录下，需要 .env 中的 DASHSCOPE_API_KEY 与网络）：
    python scripts/eval_classifier.py

输出每条问题的改写对比与总体命中率。
"""
import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.rag.classifier import classify_question

EVAL_FILE = BACKEND_DIR / "eval" / "classifier_queries.json"


def _keywords_hit(clarified: str, groups: list[list[str]]) -> bool:
    """每个关键词组至少命中一个（大小写不敏感、子串匹配）"""
    low = clarified.lower()
    return all(any(k.lower() in low for k in group) for group in groups)


def main() -> None:
    data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    categories = data["categories"]
    queries = data["queries"]
    print(f"分类员评估集: {len(queries)} 条口语化问题\n")

    kw_hits = 0
    cat_hits = 0
    cat_total = 0
    t0 = time.time()

    for item in queries:
        q = item["question"]
        result = classify_question(q, categories)
        clarified = (result or {}).get("clarified_question") or ""
        got_category = (result or {}).get("category_name") or None

        kw_ok = _keywords_hit(clarified, item["keywords"])
        kw_hits += 1 if kw_ok else 0

        expected_cat = item.get("category")
        cat_ok = None
        if expected_cat is not None:
            cat_total += 1
            cat_ok = got_category == expected_cat
            cat_hits += 1 if cat_ok else 0

        # 三态判定：期望分类为 null 时不要求分类（cat_ok 保持 None，只看关键词）
        mark_ok = kw_ok and (cat_ok is None or cat_ok)
        mark = "✅" if mark_ok else "❌"
        print(f"{mark} 原文: {q}")
        print(f"   改写: {clarified or '(未返回结果)'}")
        if expected_cat is not None:
            print(f"   分类: 期望[{expected_cat}] 实际[{got_category}]")
        else:
            print(f"   分类: 期望[不指定] 实际[{got_category}]")

    print()
    n = len(queries)
    print(f"改写关键词命中率: {kw_hits}/{n} = {kw_hits / n if n else 0.0:.0%}")
    if cat_total:
        print(f"分类准确率:       {cat_hits}/{cat_total} = {cat_hits/cat_total:.0%}")
    print(f"耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
