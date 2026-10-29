"""同义词自动发现服务（验收批次1·8g 自动化增强）

- generate_suggestions：扫最近用户提问，LLM 找"问法词 vs 文档词"错配，
  输出建议列表（仅建议，不落配置）
- 建议暂存 retrieval_suggestions.json（带 seen 状态，防重复弹出）；
  管理页"采纳"=合并进同义词配置并立即保存生效 + 留审计，"忽略"=标记已读
- auto_scan_loop：后台定时扫描（默认 24h 一次，main lifespan 启动），
  扫描结果合并进暂存（按词对去重，已读状态保留）
"""
import asyncio
import json
import threading
from pathlib import Path

from loguru import logger

from app.config import settings

SUGGESTIONS_FILE = Path(__file__).resolve().parents[2] / "config" / "retrieval_suggestions.json"

_lock = threading.Lock()


def _load() -> list[dict]:
    try:
        data = json.loads(SUGGESTIONS_FILE.read_text(encoding="utf-8"))
        return data.get("suggestions", [])
    except (OSError, json.JSONDecodeError):
        return []


def _save(items: list[dict]) -> None:
    with _lock:
        SUGGESTIONS_FILE.write_text(
            json.dumps({"suggestions": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _dedupe_key(s: dict) -> tuple:
    return (s.get("query_term", ""), s.get("doc_term", ""))


def pending_suggestions() -> list[dict]:
    """未处理（未采纳未忽略）的建议，最近的在前面"""
    items = [s for s in _load() if not s.get("seen")]
    return list(reversed(items))[:10]


def store_suggestions(suggestions: list[dict]) -> int:
    """合并新建议（按词对去重；已读状态保留），返回新增条数"""
    existing = _load()
    keys = {_dedupe_key(s) for s in existing}
    added = 0
    for s in suggestions:
        s = dict(s)
        s.setdefault("seen", False)
        key = _dedupe_key(s)
        if not key[0] or not key[1] or key in keys:
            continue
        existing.append(s)
        keys.add(key)
        added += 1
    if added:
        # 质检低危修复：暂存上限 200 条（24h/轮 × ≤10 条），超限丢最旧的已读条目
        if len(existing) > 200:
            existing = [x for x in existing if not x.get("seen")][:50] + existing[-150:]
        _save(existing)
    return added


def mark_suggestion(query_term: str, doc_term: str, seen: bool = True) -> bool:
    """采纳/忽略后标记已处理"""
    items = _load()
    for s in items:
        if _dedupe_key(s) == (query_term, doc_term):
            s["seen"] = seen
            _save(items)
            return True
    return False


async def generate_suggestions(db) -> list[dict]:
    """扫最近 50 条用户消息（去重取 30），LLM 输出建议词对（不落配置不落库）"""
    from sqlalchemy import select

    from app.models.message import Message

    rows = (await db.execute(
        select(Message.content).where(Message.role == "user").order_by(
            Message.created_at.desc()
        ).limit(50)
    )).all()
    questions = list(dict.fromkeys(r[0] for r in rows if r[0] and r[0].strip()))[:30]
    if not questions:
        return []

    prompt = (
        "你是企业知识库的词表优化助手。下面是员工最近的真实提问。\n"
        "请找出其中'员工问法用词'与'知识库文档常见用词'存在错配的词对"
        "（如员工问'礼品'而制度写'礼金'、问'报销'而文档写'费用报销'）。\n"
        "只输出你确有把握、且对检索有实际价值的词对，最多 10 条。\n"
        "严格输出 JSON，不要任何解释文字：\n"
        '{"suggestions": [{"query_term": "员工问法用词", "doc_term": "文档用词", "reason": "一句话理由"}]}\n\n'
        "员工提问：\n" + "\n".join(f"- {q}" for q in questions)
    )
    try:
        from app.rag.llm import get_llm

        llm = get_llm(streaming=False)
        # 挂清单收敛：检索建议 LLM 调用走专用池（原挂共享池）
        from app.rag.llm import run_in_llm_executor

        resp = await run_in_llm_executor(llm.invoke, prompt)
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        return [
            s for s in data.get("suggestions", [])
            if s.get("query_term") and s.get("doc_term")
        ][:10]
    except Exception as e:
        logger.warning(f"同义词建议生成失败: {e}")
        return []


async def auto_scan_loop() -> None:
    """后台定时扫描（main lifespan 启动；间隔 settings.suggest_scan_interval_hours）"""
    from app.infrastructure.database import async_session_factory

    logger.info(f"同义词自动发现已启动：每 {settings.suggest_scan_interval_hours} 小时扫描一次")
    while True:
        try:
            await asyncio.sleep(settings.suggest_scan_interval_hours * 3600)
            async with async_session_factory() as db:
                suggestions = await generate_suggestions(db)
            added = store_suggestions(suggestions)
            if added:
                logger.info(f"同义词自动扫描：新增 {added} 条建议待管理员审阅")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"同义词自动扫描失败（下轮重试）: {e}")
