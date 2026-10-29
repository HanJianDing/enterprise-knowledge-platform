"""DeepSeek 裁判模块 — LLM-as-judge（回答质量评估用）

用外部模型（DeepSeek）给 AI 客服的回答打分：外校老师判卷，
避免"千问给千问打分"的自评偏松问题。独立账号、独立计费。

失败降级策略：无密钥/网络/解析失败时返回 None，调用方自行决定
跳过该题或按 0 分处理（裁判不应阻断评估主流程）。
"""
import json
import re

import httpx
from loguru import logger

from app.config import settings

DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
JUDGE_MODEL = "deepseek-chat"


def judge_answer(
    question: str,
    answer: str,
    criteria: str,
    context: str = "",
) -> dict | None:
    """请 DeepSeek 按给定评分标准给回答打分。

    Args:
        question: 用户问题
        answer: AI 客服的回答（已按用户版后处理）
        criteria: 评分标准（裁判题型自己定义，如"是否追问了缺失的性别信息"）
        context: 参考文档内容（幻觉检测题用于核对回答是否编造文档外信息）

    Returns:
        {"score": 0~100, "passed": bool, "reason": str} 或 None（裁判不可用）
    """
    if not settings.deepseek_api_key:
        logger.warning("未配置 DEEPSEEK_API_KEY，裁判不可用")
        return None

    prompt = f"""你是严格的评分裁判。请按以下标准给"AI 客服的回答"打分。

【用户问题】
{question}

【参考文档内容】
{context or "（未提供）"}

【AI 客服的回答】
{answer}

【评分标准】
{criteria}

请只输出 JSON：{{"score": 0到100的整数, "passed": true或false, "reason": "一句话理由"}}"""

    try:
        resp = httpx.post(
            DEEPSEEK_CHAT_URL,
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": JUDGE_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,  # 裁判要稳定，不要随机性
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        # 提取 JSON（兼容 markdown 代码块包裹）
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            logger.warning(f"裁判输出无法解析: {content[:200]}")
            return None
        result = json.loads(match.group(0))
        score = result.get("score")
        passed = result.get("passed")
        # bool 是 int 子类，需显式排除；score 必须在 0~100 范围内
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not (0 <= score <= 100)
            or not isinstance(passed, bool)
        ):
            logger.warning(f"裁判输出字段不合法: {result}")
            return None
        return {"score": int(score), "passed": passed, "reason": result.get("reason", "")}
    except Exception as e:
        logger.warning(f"DeepSeek 裁判调用失败: {e}")
        return None
