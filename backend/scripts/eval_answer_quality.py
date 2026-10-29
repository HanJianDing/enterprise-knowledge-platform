"""回答质量评估脚本 — 数值准确率 + 合规率 + 延迟/token 基线

程序判定（不需要裁判）：
- 数值准确题（numeric）：完整走检索 + user 角色链生成回答，检查回答是否包含
  文档中的关键数值原文（如 "500ml"）——验证"数值必须逐字复制"提示词规则的
  端到端效果（覆盖大模型"抄串行"风险）。
- 合规题（compliance）：同链路生成回答，按 stream_chat 同款后处理
  （去引用标记 + 150字截断）后，检查是否还残留引用类违禁串。

附带记录每问的检索耗时、生成耗时与 token 用量（延迟/费用基线）。

用法（在 backend 目录下，需要 .env 中的 DASHSCOPE_API_KEY 与网络）：
    python scripts/eval_answer_quality.py

约 14 问，每问含一次真实 LLM 调用，整体 1~2 分钟。
"""
import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.rag.chain import retrieve_only, build_rag_chain
from app.rag.llm import TokenUsageHandler
from app.services.chat_service import (
    _truncate_answer,
    USER_ANSWER_MAX_CHARS,
)

EVAL_FILE = BACKEND_DIR / "eval" / "answer_checks.json"

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


def _generate(question: str) -> tuple[str, float, float, int | None]:
    """检索 + user 角色链生成，返回 (回答, 检索秒, 生成秒, total_tokens)"""
    t0 = time.time()
    docs = retrieve_only(question, top_k=6, doc_ids=_all_doc_ids())
    retrieve_s = time.time() - t0

    handler = TokenUsageHandler()
    chain = build_rag_chain(role="user", retrieved_docs=docs)
    t0 = time.time()
    answer = chain.invoke(question, config={"callbacks": [handler]})
    generate_s = time.time() - t0
    tokens = handler.usage.get("total_tokens") if handler.usage else None
    return answer, retrieve_s, generate_s, tokens


def _post_process(answer: str) -> str:
    """按 stream_chat 员工路径的同款后处理：句界软截断（保留 [来源N] 引用）"""
    cleaned, _ = _truncate_answer(answer, USER_ANSWER_MAX_CHARS)
    return cleaned


def main() -> None:
    data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    numeric_qs = data["numeric"]
    compliance_qs = data["compliance"]

    numeric_hits = 0
    retr_times: list[float] = []
    gen_times: list[float] = []
    token_list: list[int] = []

    print(f"== 数值准确题（{len(numeric_qs)} 题） ==")
    for item in numeric_qs:
        q = item["question"]
        answer, retr_s, gen_s, tokens = _generate(q)
        retr_times.append(retr_s)
        gen_times.append(gen_s)
        if tokens:
            token_list.append(tokens)
        processed = _post_process(answer)
        ok = all(sub in processed for sub in item["expected_substrings"])
        numeric_hits += 1 if ok else 0
        mark = "✅" if ok else "❌"
        print(f"{mark} {q}")
        print(f"   回答: {processed[:120]}")
        if not ok:
            print(f"   缺失期望子串: {[s for s in item['expected_substrings'] if s not in processed]}")

    compliance_hits = 0
    print(f"\n== 合规题（{len(compliance_qs)} 题） ==")
    for item in compliance_qs:
        q = item["question"]
        answer, retr_s, gen_s, tokens = _generate(q)
        retr_times.append(retr_s)
        gen_times.append(gen_s)
        if tokens:
            token_list.append(tokens)
        processed = _post_process(answer)
        bad = [b for b in item["banned_substrings"] if b in processed]
        ok = not bad
        compliance_hits += 1 if ok else 0
        mark = "✅" if ok else "❌"
        print(f"{mark} {q}")
        print(f"   回答: {processed[:120]}")
        if bad:
            print(f"   残留违禁串: {bad}")

    n_num, n_com = len(numeric_qs), len(compliance_qs)
    print()
    print(f"数值准确率: {numeric_hits}/{n_num} = {numeric_hits / n_num if n_num else 0:.0%}")
    print(f"合规率:     {compliance_hits}/{n_com} = {compliance_hits / n_com if n_com else 0:.0%}")
    if retr_times:
        print(f"延迟基线:   单问检索平均 {sum(retr_times) / len(retr_times) * 1000:.0f}ms | "
              f"单问生成平均 {sum(gen_times) / len(gen_times) * 1000:.0f}ms")
    if token_list:
        print(f"费用基线:   单问生成平均 {sum(token_list) / len(token_list):.0f} tokens（模型回报）")
    else:
        print("费用基线:   token 用量未被模型回报（可后续用 API 账单或计费接口补充）")


if __name__ == "__main__":
    main()
