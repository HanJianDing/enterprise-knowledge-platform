"""评估集数据质量测试 — P3-4：保证评估集结构有效，评估脚本可跑

验收批次1：评估集分层（queries_regression.json + queries_long.json），
校验改为"每层各自有效 + 合并后分桶达标"。
"""
import json
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
SET_FILES = {
    "regression": EVAL_DIR / "queries_regression.json",
    "long": EVAL_DIR / "queries_long.json",
}


def _load_all() -> list:
    data = []
    for f in SET_FILES.values():
        data.extend(json.loads(f.read_text(encoding="utf-8")))
    return data


def test_eval_dataset_loads_and_has_min_size():
    """分层评估集：每层文件可解析，合并后至少 20 条"""
    for name, f in SET_FILES.items():
        data = json.loads(f.read_text(encoding="utf-8"))
        assert isinstance(data, list) and len(data) >= 5, f"{name} 层题目过少"
    assert len(_load_all()) >= 20


def test_eval_dataset_items_valid():
    """每条：question 非空、expected_docs 为非空字符串列表、问题不重复（跨层全局不重复）"""
    questions = []
    for item in _load_all():
        assert isinstance(item.get("question"), str) and item["question"].strip()
        expected = item.get("expected_docs")
        assert isinstance(expected, list) and len(expected) >= 1
        assert all(isinstance(d, str) and d for d in expected)
        # 模糊题标记必须是布尔值
        assert isinstance(item.get("fuzzy", False), bool)
        questions.append(item["question"])
    assert len(set(questions)) == len(questions), "评估集存在重复问题"


def test_eval_dataset_has_both_buckets():
    """标准题与模糊题（fuzzy=true）都至少 5 条，保证两类指标有统计意义"""
    data = _load_all()
    standard = [q for q in data if not q.get("fuzzy")]
    fuzzy = [q for q in data if q.get("fuzzy")]
    assert len(standard) >= 5
    assert len(fuzzy) >= 5


def test_approval_dataset_valid():
    """审批规则评估集（批次2-4）：字段完整、期望值非空、题不重复"""
    f = EVAL_DIR / "queries_approval.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) >= 5
    seen = set()
    for item in data:
        assert isinstance(item.get("question"), str) and item["question"].strip()
        assert isinstance(item.get("expected_approver"), str) and item["expected_approver"]
        assert item.get("expected_action") in ("审批", "备案")
        assert item["question"] not in seen, "审批规则评估集存在重复问题"
        seen.add(item["question"])


def test_relation_dataset_valid():
    """关系层评估集（批次2-2）：字段完整、kind 合法、期望值按 kind 配套、题不重复"""
    f = EVAL_DIR / "queries_relation.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) >= 20
    seen = set()
    kinds = {"path", "neighbors", "no_intervention", "community"}
    for item in data:
        assert isinstance(item.get("question"), str) and item["question"].strip()
        assert item.get("kind") in kinds, f"未知 kind: {item.get('kind')}"
        if item["kind"] == "path":
            assert item.get("expected_path"), "路径题必须给期望路径"
            assert item.get("expected_entities"), "路径题必须给期望实体链接"
        elif item["kind"] == "neighbors":
            assert item.get("expected_neighbors"), "邻居题必须给期望邻居"
            assert item.get("expected_entities"), "邻居题必须给期望实体链接"
        elif item["kind"] == "community":
            assert item.get("expected_members"), "社区题必须给期望成员"
            # 批次2-5：社区级问法（"研发团队有哪些人"）无实体锚定，可为空数组
            assert "expected_entities" in item, "社区题必须给实体链接字段"
        assert item["question"] not in seen, "关系层评估集存在重复问题"
        seen.add(item["question"])


# ── 口语改写题（分类员评测）数据集校验 ──

CLASSIFIER_EVAL_FILE = Path(__file__).resolve().parent.parent / "eval" / "classifier_queries.json"


def test_classifier_eval_dataset_valid():
    """分类员评估集：分类列表字段完整、题目 ≥ 8 条、期望分类都在分类名单内"""
    data = json.loads(CLASSIFIER_EVAL_FILE.read_text(encoding="utf-8"))
    cats = data.get("categories")
    assert isinstance(cats, list) and len(cats) >= 3
    cat_names = set()
    for c in cats:
        for key in ("id", "name", "path"):
            assert isinstance(c.get(key), str) and c[key], f"分类缺字段: {c}"
        cat_names.add(c["name"])
    # 分类 id/name 必须唯一（重名会被集合静默合并，掩盖数据错误）
    assert len(cat_names) == len(cats), "分类 name 存在重复"
    assert len({c["id"] for c in cats}) == len(cats), "分类 id 存在重复"

    queries = data.get("queries")
    assert isinstance(queries, list) and len(queries) >= 8
    seen = set()
    for item in queries:
        assert isinstance(item.get("question"), str) and item["question"].strip()
        groups = item.get("keywords")
        assert isinstance(groups, list) and len(groups) >= 1
        for group in groups:
            assert isinstance(group, list) and all(isinstance(k, str) and k for k in group)
        expected_cat = item.get("category")
        assert expected_cat is None or expected_cat in cat_names, (
            f"期望分类不在分类名单内: {expected_cat}"
        )
        assert item["question"] not in seen, "分类员评估集存在重复问题"
        seen.add(item["question"])


# ── 回答质量评估（数值准确 + 合规）数据集校验 ──

ANSWER_CHECKS_FILE = Path(__file__).resolve().parent.parent / "eval" / "answer_checks.json"


def test_answer_checks_dataset_valid():
    """回答质量评估集：numeric ≥5 条且每条的期望子串非空；compliance ≥3 条且违禁串非空"""
    data = json.loads(ANSWER_CHECKS_FILE.read_text(encoding="utf-8"))
    numeric = data.get("numeric")
    compliance = data.get("compliance")
    assert isinstance(numeric, list) and len(numeric) >= 5
    seen = set()
    for item in numeric:
        assert isinstance(item.get("question"), str) and item["question"].strip()
        subs = item.get("expected_substrings")
        assert isinstance(subs, list) and len(subs) >= 1
        assert all(isinstance(s, str) and s for s in subs)
        assert item["question"] not in seen, "数值题存在重复问题"
        seen.add(item["question"])

    assert isinstance(compliance, list) and len(compliance) >= 3
    for item in compliance:
        assert isinstance(item.get("question"), str) and item["question"].strip()
        banned = item.get("banned_substrings")
        assert isinstance(banned, list) and len(banned) >= 1
        assert all(isinstance(s, str) and s for s in banned)


# ── 裁判式评估数据集校验 ──

JUDGE_CHECKS_FILE = Path(__file__).resolve().parent.parent / "eval" / "judge_checks.json"


def test_judge_checks_dataset_valid():
    """裁判式评估集：三类题数量达标、字段完整、criteria 非空"""
    data = json.loads(JUDGE_CHECKS_FILE.read_text(encoding="utf-8"))
    followup = data.get("followup")
    hallucination = data.get("hallucination")
    multiturn = data.get("multiturn")

    assert isinstance(followup, list) and len(followup) >= 5
    assert isinstance(hallucination, list) and len(hallucination) >= 5
    assert isinstance(multiturn, list) and len(multiturn) >= 4

    seen = set()
    for item in followup + hallucination:
        assert isinstance(item.get("question"), str) and item["question"].strip()
        assert isinstance(item.get("criteria"), str) and item["criteria"].strip()
        assert item["question"] not in seen, "单轮裁判题存在重复问题"
        seen.add(item["question"])

    for item in multiturn:
        for key in ("round1", "round2"):
            assert isinstance(item.get(key), str) and item[key].strip()
        assert isinstance(item.get("criteria"), str) and item["criteria"].strip()
