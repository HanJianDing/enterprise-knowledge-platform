"""审批链规则存储与查询服务（批次2-4 规则腿）

- parse_and_store_for_document：文档处理完成后挂钩——解析全文 →
  幂等写规则表（先删该文档旧规则再写，重传/重解析无残留）
- delete_rules_for_document：文档删除时同步清理
- match_question：问句 → 规则答案（内存解析表规则后匹配；
  规则量级小，全量查询可接受）
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.database import async_session_factory
from app.rag.rules.matcher import _EPOCH, format_rule_text, match
from app.rag.rules.parser import Rule, parse_text

logger = logging.getLogger(__name__)


def _rule_sort_key(effective_date: datetime | None, doc_created_at: datetime | None) -> datetime:
    """冲突裁决时间键：生效日期优先；无日期（"自发布之日起生效"=现行制度）
    回退文档上传时间；naive 假定 UTC 附加时区（防 naive/aware 比较崩溃）"""
    key = effective_date or doc_created_at or _EPOCH
    if getattr(key, "tzinfo", None) is None:
        return key.replace(tzinfo=timezone.utc)
    return key


async def _doc_full_text(doc_id: str) -> str | None:
    """文档全量分块文本拼接（解析输入；分块在 Chroma，doc_id 过滤取出）。
    质检 R8：Chroma 查询是阻塞调用，放线程池防事件循环卡顿。"""
    import asyncio

    try:
        from app.rag.retriever import get_chunks_by_document

        chunks = await asyncio.to_thread(get_chunks_by_document, doc_id)
        # 批次11a：排除特殊块——doc_summary（目录块）与 raptor_summary
        # （摘要块）不是制度原文，摘要截断文本会产出错误区间规则；
        # sub_chunk 与父块内容重复，会产出重复规则（下方去重兜底，
        # 此处一并排除省解析开销）
        texts = [
            c["content"] for c in chunks
            if c.get("content") and c.get("chunk_type") not in (
                "doc_summary", "raptor_summary", "sub_chunk",
            )
        ]
        return "\n".join(texts) if texts else None
    except Exception as e:
        logger.warning(f"规则解析取文档文本失败（{doc_id}）: {e}")
        return None


async def parse_and_store_for_document(doc_id: str) -> int:
    """解析文档审批规则并幂等入库。返回写入规则数（0=无匹配句式）。

    质检 R7：解析出 0 条规则时同样执行删除——旧版本规则不留残留
    （文档重传后原规则已不适用，宁缺勿滥）。"""
    try:
        text = await _doc_full_text(doc_id)
        if text is None:
            return 0
        rules = parse_text(text)
        # 去重（同一句式在文本重复时可能解析出相同规则；按业务+区间+审批人+动作）
        deduped: list[Rule] = []
        seen_keys: set[tuple] = set()
        for r in rules:
            key = (r.business_type, r.min_value, r.max_value, r.unit,
                   r.approver, r.action)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(r)
        rules = deduped

        from app.models.rule import ApprovalRule

        async with async_session_factory() as db:
            await db.execute(delete(ApprovalRule).where(
                ApprovalRule.source_doc_id == doc_id
            ))
            for r in rules:
                db.add(ApprovalRule(
                    id=str(uuid.uuid4()),
                    business_type=r.business_type,
                    min_value=r.min_value,
                    max_value=r.max_value,
                    unit=r.unit,
                    approver=r.approver,
                    action=r.action,
                    source_doc_id=doc_id,
                    source_text=format_rule_text(r),
                    effective_date=r.effective_date,
                ))
            await db.commit()
        logger.info(f"审批规则入库: {doc_id} → {len(rules)} 条")
        return len(rules)
    except Exception as e:
        # 质检 R1：fire-and-forget 任务顶层 try/except（对齐图谱腿惯例——
        # 失败只留日志，不产生未回收任务异常噪音）
        logger.warning(f"审批规则解析入库失败（{doc_id}）: {e}")
        return 0


async def find_conflicts() -> list[dict]:
    """规则冲突检测（批次3 初版=同区间完全相等；批次3.5 扩展=部分重叠）：
    **跨文档** + 同业务 + 同单位 + **区间重叠（含完全相等与部分重叠）**
    + 审批人/动作不同的规则对。
    真实场景=旧版与新版制度并存（旧版存档），输出冲突对、重叠段与裁决方向
    （effective_date 优先，无则回退上传时间——"新制度覆盖旧制度"）。
    同文档内相邻区间边界重叠（"1000 元以内"与"1000 至 5000 元"）不是冲突
    （首轮实测 18 对噪音即此因）——同文档规则是同一制度的分档设计。
    部分重叠示例（用户 2026-10-08 拍板防患于未然）：旧"3000 至 8000 元"
    vs 新"5000 元以上"→ 重叠段 [5000, 8000] 冲突，裁决归新文档。"""
    try:
        from sqlalchemy import asc

        from app.models.document import Document
        from app.models.rule import ApprovalRule

        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    select(ApprovalRule, Document.created_at, Document.original_name)
                    .outerjoin(Document, Document.id == ApprovalRule.source_doc_id)
                    .order_by(asc(ApprovalRule.business_type))
                )
            ).all()
        rules = [
            {
                "business_type": r.business_type,
                "min_value": r.min_value,
                "max_value": r.max_value,
                "unit": r.unit,
                "approver": r.approver,
                "action": r.action,
                "source_text": r.source_text,
                "doc_name": doc_name or "已删除文档",
                "doc_id": r.source_doc_id,
                # 裁决键：生效日期优先，无日期（"自发布之日起生效"=现行制度）
                # 回退文档上传时间；naive/aware 统一按 UTC 处理
                "effective_date": _rule_sort_key(r.effective_date, doc_created_at),
            }
            for r, doc_created_at, doc_name in rows
        ]

        conflicts: list[dict] = []
        for i, a in enumerate(rules):
            for j, b in enumerate(rules):
                if i >= j:
                    continue
                # 冲突前提：跨文档 + 同业务同单位 + 区间重叠 + 审批人/动作不同
                if a["doc_id"] == b["doc_id"]:
                    continue
                if a["business_type"] != b["business_type"] or a["unit"] != b["unit"]:
                    continue
                if a["approver"] == b["approver"] and a["action"] == b["action"]:
                    continue
                # 区间重叠判定（None=∞ 语义）
                a_lo = a["min_value"] if a["min_value"] is not None else 0.0
                a_hi = a["max_value"] if a["max_value"] is not None else float("inf")
                b_lo = b["min_value"] if b["min_value"] is not None else 0.0
                b_hi = b["max_value"] if b["max_value"] is not None else float("inf")
                if a_lo > b_hi or b_lo > a_hi:
                    continue  # 无重叠
                # 冲突形态判定（防误报四原则）：
                # ①区间包含关系：action 不同（"审批"vs"备案"）=同制度阶梯设计
                #   （请假"三天及以上审批"⊇"七天以上备案"），不报；
                #   action 相同且审批人不同=**门槛平移型版本冲突**
                #   （旧"3000 以上：部门负责人审批"vs 新"5000 以上：财务总监审批"），
                #   重叠段 [5000,∞) 两版打架——质检 3.5 抓出的盲区，必须报
                # ②点接触（重叠段零宽度，如"1000 元以内"与"1000 至 5000 元"
                #   在 1000 边界）=相邻分档，不报
                # ③完全相等 / 互不包含的真重叠（旧"3000 至 8000"vs 新"5000 以上"）
                #   =版本冲突，报
                a_contains_b = a_lo <= b_lo and a_hi >= b_hi
                b_contains_a = b_lo <= a_lo and b_hi >= a_hi
                if (a_contains_b or b_contains_a) and not (
                    a_lo == b_lo and a_hi == b_hi
                ):
                    # 包含关系：动作不同=阶梯设计不报；同动作审批人不同=门槛平移报
                    if a["action"] != b["action"]:
                        continue
                    # 动作相同 → 落入下方冲突输出（门槛平移）
                overlap_lo = max(a_lo, b_lo)
                overlap_hi = min(a_hi, b_hi)
                if overlap_hi - overlap_lo <= 0 and not (
                    a_lo == b_lo and a_hi == b_hi
                ):
                    continue  # 点接触（零宽度且非完全相等）
                if overlap_lo <= 0:
                    range_start = "全部"
                else:
                    range_start = f"{overlap_lo:g}"
                overlap_text = (
                    f"{range_start} 至 {overlap_hi:g} {a['unit']}"
                    if overlap_hi != float("inf")
                    else (f"{range_start}{a['unit']}以上"
                          if overlap_lo > 0 else f"全部{a['unit']}")
                )
                newer_is_a = a["effective_date"] > b["effective_date"]
                conflicts.append({
                    "business_type": a["business_type"],
                    "unit": a["unit"],
                    "overlap": overlap_text,
                    "rule_a": {"source_text": a["source_text"], "doc_name": a["doc_name"],
                               "approver": a["approver"]},
                    "rule_b": {"source_text": b["source_text"], "doc_name": b["doc_name"],
                               "approver": b["approver"]},
                    "resolved_approver": a["approver"] if newer_is_a else b["approver"],
                    "resolved_doc": a["doc_name"] if newer_is_a else b["doc_name"],
                })
        return conflicts
    except SQLAlchemyError as e:
        # 批次4 异常收窄：DB 故障静默回退空列表；编程错误自然抛出
        # （此前裸 except 曾两次吞掉 NameError 静默回退）
        logger.warning(f"规则冲突检测失败（{type(e).__name__}: {e}）")
        return []


async def delete_rules_for_document(doc_id: str) -> int:
    """文档删除时清理其规则（增量更新，与图谱子图清理同哲学）"""
    from app.models.rule import ApprovalRule

    async with async_session_factory() as db:
        result = await db.execute(delete(ApprovalRule).where(
            ApprovalRule.source_doc_id == doc_id
        ))
        await db.commit()
    return result.rowcount or 0


async def match_question(question: str) -> dict | None:
    """问句 → 规则答案。全量规则表加载后匹配（规则量小，无分页需求）。"""
    try:
        from sqlalchemy import asc

        from app.models.document import Document
        from app.models.rule import ApprovalRule

        async with async_session_factory() as db:
            # 质检 R2：显式排序（匹配裁决不依赖行序，排序仅保证输出稳定）
            # 批次3：join 文档表带出 created_at——同区间冲突时"新制度覆盖旧制度"
            rows = (
                await db.execute(
                    select(ApprovalRule, Document.created_at)
                    .outerjoin(Document, Document.id == ApprovalRule.source_doc_id)
                    .order_by(asc(ApprovalRule.created_at), asc(ApprovalRule.id))
                )
            ).all()
        rules = [
            Rule(
                business_type=r.business_type,
                min_value=r.min_value,
                max_value=r.max_value,
                unit=r.unit,
                approver=r.approver,
                action=r.action,
                effective_date=r.effective_date,
                created_at=doc_created_at,
            )
            for r, doc_created_at in rows
        ]
        if not rules:
            return None
        hit = match(rules, question)
        if hit is None:
            return None
        return hit
    except SQLAlchemyError as e:
        # 批次4 异常收窄：DB 故障回退向量检索；编程错误自然抛出
        logger.warning(f"规则匹配失败，回退向量检索: {e}")
        return None
