"""裁判式评估脚本 — 追问质量 / 幻觉检测 / 多轮连贯（DeepSeek 当裁判）

三类题均走真实链路（检索 + user 角色链生成 + 用户版后处理），
再请 DeepSeek 按每题 criteria 打分（外校老师判卷，避免千问自评偏松）。

- followup：单轮问答，判"信息不足时是否追问了关键信息"
- hallucination：单轮问答，裁判对照参考文档判"是否编造文档外内容"
- multiturn：两轮问答（第二轮带第一轮历史上下文），判"指代/上下文是否连贯"

裁判不可用（None）时该题记为 skipped，不按失败计。

用法（在 backend 目录下，需要 .env 的 DASHSCOPE_API_KEY 与 DEEPSEEK_API_KEY）：
    python scripts/eval_judge_quality.py

共 26 题（含 8 组多轮=16 次生成），约 2~4 分钟。
"""
import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.rag.chain import retrieve_only, build_rag_chain
from app.rag.judge import judge_answer
from app.rag.reranker import rerank_documents
from app.services.chat_service import (
    _truncate_answer,
    _with_history_context,
    USER_ANSWER_MAX_CHARS,
)

EVAL_FILE = BACKEND_DIR / "eval" / "judge_checks.json"
RECALL_K = 20   # 与生产 stream_chat 一致：粗召回 20 条
ANSWER_K = 6    # rerank 后取 6 条

_ALL_DOC_IDS: list[str] | None = None


def _load_all_doc_ids() -> list[str]:
    """评估以管理员视角跑全库：加载全部文档 ID 作为检索可见范围。

    企业知识库阶段1：检索接口要求必传可见文档范围（fail-closed 防越权）。
    """
    import asyncio
    from sqlalchemy import select
    from app.infrastructure.database import async_session_factory
    from app.models.document import Document

    async def _run() -> list[str]:
        async with async_session_factory() as db:
            result = await db.execute(select(Document.id))
            return [row[0] for row in result.all()]

    return asyncio.run(_run())


def _all_doc_ids() -> list[str]:
    global _ALL_DOC_IDS
    if _ALL_DOC_IDS is None:
        _ALL_DOC_IDS = _load_all_doc_ids()
    return _ALL_DOC_IDS


def _generate(question: str, chat_history: list | None = None) -> tuple[str, str]:
    """按生产链路模拟：检索词带历史上下文（多轮检索）→ 粗召回 20 → rerank 取 6 →
    user 角色链生成 → 截断后应用程序兜底追问（与 stream_chat 一致）。
    返回 (回答(已后处理), 检索文档拼接上下文)"""
    retrieval_query = _with_history_context(question, chat_history)
    docs = retrieve_only(retrieval_query, top_k=RECALL_K, doc_ids=_all_doc_ids())
    if len(docs) > ANSWER_K:
        docs = rerank_documents(question, docs, ANSWER_K)
    else:
        docs = docs[:ANSWER_K]
    chain = build_rag_chain(chat_history=chat_history, role="user", retrieved_docs=docs)
    answer = chain.invoke(question)
    # 员工路径后处理：句界软截断（保留 [来源N] 引用）。
    # 注：客服时代的代码兜底追问已随客服功能剥离，追问交给提示词与模型。
    processed, _ = _truncate_answer(answer, USER_ANSWER_MAX_CHARS)
    context = "\n".join(d.page_content for d in docs)
    return processed, context


def _judge(item: dict, answer: str, context: str, question_key: str) -> dict | None:
    """单题裁判，返回 {"passed", "score", "reason"} 或 None"""
    return judge_answer(
        question=item[question_key],
        answer=answer,
        criteria=item["criteria"],
        context=context,
    )


def _run_single_round(items: list, label: str, question_key: str = "question") -> None:
    """单轮类题型（followup / hallucination）"""
    print(f"\n== {label}（{len(items)} 题） ==")
    passed = skipped = 0
    scores: list[int] = []
    for item in items:
        q = item[question_key]
        t0 = time.time()
        answer, context = _generate(q)
        verdict = _judge(item, answer, context, question_key)
        elapsed = time.time() - t0
        if verdict is None:
            skipped += 1
            print(f"⏭️  [裁判不可用] {q}")
            print(f"   回答: {answer[:100]}")
            continue
        if verdict["passed"]:
            passed += 1
        scores.append(verdict["score"])
        mark = "✅" if verdict["passed"] else "❌"
        print(f"{mark} [score={verdict['score']}] {q}")
        print(f"   回答: {answer[:100]}")
        print(f"   裁判: {verdict['reason'][:100]}")
        print(f"   （{elapsed:.1f}s）")

    n = len(items) - skipped
    if n:
        print(f"  通过率: {passed}/{n} = {passed / n:.0%} | 平均分 {sum(scores) / len(scores):.0f}")
    if skipped:
        print(f"  （裁判不可用跳过 {skipped} 题）")


def _run_multiturn(items: list) -> None:
    """两轮类题型：第二轮带第一轮历史，判连贯性。

    公平性：裁判需要看到第一轮问答才能判断第二轮的"哪些/这些"是否承接——
    所以把第一轮对话并入裁判的参考上下文。
    """
    print(f"\n== 多轮连贯题（{len(items)} 组，每组两轮） ==")
    passed = skipped = 0
    scores: list[int] = []
    for item in items:
        r1, r2 = item["round1"], item["round2"]
        t0 = time.time()
        answer1, _ = _generate(r1)
        history = [
            {"role": "user", "content": r1},
            {"role": "assistant", "content": answer1},
        ]
        answer2, context2 = _generate(r2, chat_history=history)
        # 裁判上下文 = 第二轮检索文档 + 第一轮对话（供裁判核对指代）
        judge_context = (
            f"【第一轮对话】\n用户: {r1}\n客服: {answer1}\n\n"
            f"【第二轮参考文档】\n{context2}"
        )
        verdict = judge_answer(
            question=r2,
            answer=answer2,
            criteria=item["criteria"],
            context=judge_context,
        )
        elapsed = time.time() - t0
        if verdict is None:
            skipped += 1
            print(f"⏭️  [裁判不可用] 第1轮: {r1} → 第2轮: {r2}")
            continue
        if verdict["passed"]:
            passed += 1
        scores.append(verdict["score"])
        mark = "✅" if verdict["passed"] else "❌"
        print(f"{mark} [score={verdict['score']}] 第1轮: {r1}")
        print(f"   第1轮回答: {answer1[:80]}")
        print(f"   第2轮: {r2}")
        print(f"   第2轮回答: {answer2[:100]}")
        print(f"   裁判: {verdict['reason'][:100]}")
        print(f"   （{elapsed:.1f}s）")

    n = len(items) - skipped
    if n:
        print(f"  通过率: {passed}/{n} = {passed / n:.0%} | 平均分 {sum(scores) / len(scores):.0f}")
    if skipped:
        print(f"  （裁判不可用跳过 {skipped} 组）")


def main() -> None:
    data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    t0 = time.time()
    _run_single_round(data["followup"], "追问质量题")
    _run_single_round(data["hallucination"], "幻觉检测题")
    _run_multiturn(data["multiturn"])
    print(f"\n总耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
