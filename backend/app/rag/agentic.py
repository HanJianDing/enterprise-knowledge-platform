"""Agentic RAG 轻量编排（产品化 B1）

借鉴 RAGFlow Deep Search 的思路，但裁剪为固定三步（拆解 → 多路检索 → 汇总）：
- 不做循环式 agent（无"多轮 agent 自主循环"）：复杂问题最多一次拆解 +
  每子问题一次检索，天然防失控，延迟可预期（拆解与计算各 1 次轻量模型调用）。
  批次C·11b 增补：反思循环（reflect_answer）为"生成后单次自检+单次修正
  重试"的固定两步——非循环式、单次上限，与裁剪哲学一致；
- 计算类问题由程序精确算数（LLM 只负责提取数值与算式，不负责算——大模型算数不可靠）；
- 表达式求值用 ast 白名单（仅允许数字与四则运算），杜绝 eval 注入面。
"""
import ast
import json
import math
import re

from loguru import logger

from app.config import settings
from app.rag.llm import get_llm

MAX_SUB_QUESTIONS = 4   # 拆解上限：防失控（子问题越多检索越多、上下文越乱）
MAX_CONTEXT_DOCS = 12   # 多路检索合并后的上下文条数上限（之后仍会过 rerank 精排）

DECOMPOSE_PROMPT = """你是企业知识库的复杂问题拆解助手。

把员工的复杂问题拆解为 2~4 个互相独立、适合向量检索的子问题。要求：
- 每个子问题自成一体（补全指代与上下文，不能出现"另一个""前者"等模糊词）
- 比较类问题：为每个比较对象各拆一个子问题
- 计算类问题：为每个参与计算的数值各拆一个子问题
- 不要输出解释文字，只输出 JSON

用户问题：
{question}

输出格式（严格遵守，不要包含 ```json 包裹）：
{{"sub_questions": ["子问题1", "子问题2"]}}"""

EXTRACT_CALC_PROMPT = """你是数值提取助手。从给出的知识库片段中提取回答用户问题所需的数值，并写出算术表达式。

要求：
- 只使用片段中明确出现的数值，禁止猜测
- 表达式只允许使用数字与 + - * / ( )，例如 "10 - 3"、"8 * 2"
- 如果片段中找不到所需数值，value 返回 null
- 不要输出解释文字，只输出 JSON

用户问题：
{question}

知识库片段：
{context}

输出格式（严格遵守）：
{{"expression": "算术表达式或null", "explanation": "一句话说明算式的含义"}}"""


# ── 安全表达式求值（ast 白名单） ──

_ALLOWED_AST_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd,
)


def safe_eval_expression(expr: str) -> int | float | None:
    """只允许数字与 + - * / () 的表达式求值（ast 白名单，无任何函数/名字访问）。

    返回值非有限数（除零、溢出）或含非法节点时返回 None——调用方按"计算失败"处理。
    """
    if not expr or len(expr) > 100:
        return None
    try:
        node = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    for n in ast.walk(node):
        if not isinstance(n, _ALLOWED_AST_NODES):
            return None
    try:
        result = eval(compile(node, "<agentic_expr>", "eval"), {"__builtins__": {}})
    except Exception:
        return None
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        return None
    if not math.isfinite(float(result)):
        return None
    return result


# ── 拆解与计算（LLM 调用，失败回落） ──

def decompose_question(question: str) -> list[str] | None:
    """把复杂问题拆解为 2~4 个独立子问题；LLM 失败/解析失败返回 None（调用方回落单路检索）"""
    try:
        llm = get_llm(streaming=False, model=settings.classifier_model)
        response = llm.invoke(DECOMPOSE_PROMPT.format(question=question))
        raw = response.content if hasattr(response, "content") else str(response)
        json_match = re.search(r'\{[^{}]*"sub_questions"[^{}]*\}', raw, re.DOTALL)
        if not json_match:
            logger.warning("拆解输出无 JSON: {}", raw[:200])
            return None
        data = json.loads(json_match.group(0))
        subs = [
            str(s).strip() for s in (data.get("sub_questions") or [])
            if s and str(s).strip()
        ][:MAX_SUB_QUESTIONS]
        if len(subs) < 2:
            logger.info("拆解出的子问题不足 2 个，按普通问题处理")
            return None
        logger.info(f"复杂问题拆解 → {len(subs)} 个子问题: {subs[:2]}...")
        return subs
    except Exception as e:
        logger.warning("复杂问题拆解失败（回落普通检索）: {}", e)
        return None


def extract_calculation(question: str, context: str) -> dict | None:
    """从检索片段提取数值并程序化计算；任一步失败返回 None（回答仍照常生成，只是不带计算结果）"""
    try:
        llm = get_llm(streaming=False, model=settings.classifier_model)
        response = llm.invoke(EXTRACT_CALC_PROMPT.format(
            question=question, context=context[:6000],
        ))
        raw = response.content if hasattr(response, "content") else str(response)
        json_match = re.search(r'\{[^{}]*"expression"[^{}]*\}', raw, re.DOTALL)
        if not json_match:
            logger.warning("数值提取输出无 JSON: {}", raw[:200])
            return None
        data = json.loads(json_match.group(0))
        expression = data.get("expression")
        if not expression or str(expression).lower() in ("null", "none"):
            return None
        result = safe_eval_expression(str(expression))
        if result is None:
            logger.warning("算式求值失败: {}", expression)
            return None
        explanation = (data.get("explanation") or "").strip()
        logger.info(f"程序计算: {expression} = {result}")
        return {"expression": str(expression), "result": result, "explanation": explanation}
    except Exception as e:
        logger.warning("数值提取失败（跳过计算）: {}", e)
        return None


def format_calculation_note(calc: dict) -> str:
    """计算结果转成注入上下文的说明文本（结果由程序精确计算，非模型心算）"""
    result = calc["result"]
    result_text = f"{int(result)}" if isinstance(result, float) and result.is_integer() else str(result)
    if calc.get("explanation"):
        return f"【程序计算结果】{calc['explanation']}：{calc['expression']} = {result_text}（由程序精确计算）"
    return f"【程序计算结果】{calc['expression']} = {result_text}（由程序精确计算）"


# ── 多路检索结果合并 ──

def merge_documents(doc_lists: list[list]) -> list:
    """多路子问题检索结果合并去重（按 document_id + chunk_index），保持出现顺序"""
    seen: set[tuple] = set()
    merged = []
    for docs in doc_lists:
        for doc in docs:
            key = (
                str(doc.metadata.get("document_id") or ""),
                doc.metadata.get("chunk_index"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(doc)
    return merged


# ── 反思循环（批次C·11b：可配置开关；编排类问题生成后自检）──

REFLECTION_PROMPT = """你是企业知识库问答的质检员。检查下面这份回答是否合格。

问题：
{question}

需要覆盖的子问题：
{sub_questions}

待检回答：
{answer}

检查标准：
1. 回答是否逐一覆盖了所有子问题（漏答=不合格）
2. 回答中的引用标注（[来源N] 等）是否指向真实存在的引用（编造编号=不合格）
3. 回答是否与知识库内容矛盾、或凭空编造了知识库没有的信息（编造=不合格）
4. 字数是否明显不足（敷衍=不合格）

只输出 JSON，不要解释文字：
{{"pass": true/false, "feedback": "不合格时的具体修正意见；合格时为空字符串"}}"""


async def reflect_answer(
    question: str,
    answer: str,
    sub_questions: list[str],
) -> dict | None:
    """生成后自检（LLM-as-judge 同款：程序问、模型判）。

    返回 {"pass": bool, "feedback": str, "usage": dict|None}（usage 为
    自检调用的 token 用量，批次C.5 起并入统计口径）；任何异常返回
    None（静默跳过，反思是增强不是阻塞）。temperature=0（判断类调用
    确定性）。
    """
    from app.rag.llm import get_llm, invoke_with_timeout, TokenUsageHandler
    from app.rag.tools import _extract_json

    subs_text = "\n".join(f"- {s}" for s in sub_questions) if sub_questions else "（无拆解，整体回答）"
    prompt = REFLECTION_PROMPT.format(
        question=question,
        sub_questions=subs_text,
        answer=answer[:4000],  # 超长答案截检（自检看结构与覆盖，不需要全文）
    )
    try:
        llm = get_llm(streaming=False, temperature=0)
        # 批次C.5 P3-12：反思调用挂 usage handler——token 用量并入统计口径
        usage_handler = TokenUsageHandler()
        resp = await invoke_with_timeout(llm, prompt, callbacks=[usage_handler])
        if resp is None:
            return None
        raw = resp.content if hasattr(resp, "content") else str(resp)
        data = _extract_json(raw)
        if data is None or "pass" not in data:
            return None
        # 批次C 质检 P3：严格解析 pass 字段——LLM 输出字符串 "false" 时
        # bool("false")=True 会误判（字符串恒真）；只认真布尔或 "true"/"false"
        raw_pass = data.get("pass")
        if isinstance(raw_pass, bool):
            verdict = raw_pass
        elif isinstance(raw_pass, str) and raw_pass.strip().lower() in ("true", "false"):
            verdict = raw_pass.strip().lower() == "true"
        else:
            return None  # 无法判定的输出：静默跳过（不误重试）
        return {
            "pass": verdict,
            "feedback": str(data.get("feedback") or "").strip(),
            "usage": usage_handler.usage,
        }
    except Exception as e:
        logger.warning("反思自检失败（静默跳过）: {}", e)
        return None
