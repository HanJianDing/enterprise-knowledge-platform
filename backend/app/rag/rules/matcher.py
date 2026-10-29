"""审批链规则匹配器（批次2-4 规则腿）——问句 → 确定性规则答案

"报销 8000 元谁审批" → 业务=报销、金额=8000 → 规则表区间命中
→ {"approver": "财务总监", "action": "审批", ...}

匹配不了返回 None（上层静默回退向量检索——规则腿答不了不硬答）。
"""
import re
from datetime import datetime, timezone

from .parser import Rule, _BUSINESS_TYPES, UNIT_FACTOR, _cn_to_float

# created_at 缺失（解析器纯产物）视为最旧的时间锚
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# 问句金额提取："8000 元"/"3 万元"/"5千元"
_QUERY_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(万|千|百)?元")
# 问句天数提取："3 天"/"三天"
_QUERY_DAYS = re.compile(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十]+)\s*天")


def extract_query_value(question: str) -> tuple[str | None, float | None]:
    """从问句提取 (单位: 元/天, 数值)。金额与天数都有时金额优先；
    都没有返回 (None, None)——匹配器将无法匹配（回退向量）。"""
    amt = _QUERY_AMOUNT.search(question)
    if amt:
        num = float(amt.group(1)) * UNIT_FACTOR.get(amt.group(2) or "", 1.0)
        return "元", num
    days = _QUERY_DAYS.search(question)
    if days:
        # 中文数字转换统一走 parser._cn_to_float（"十天"=10、"十三"=13、"二十三"=23；
        # 质检 R5/R6：此前两处各写一份转换且算法不一致，"二十三"会算成 30）
        value = _cn_to_float(days.group(1))
        return ("天", value) if value is not None else (None, None)
    return None, None


def match(rules: list[Rule], question: str) -> dict | None:
    """问句匹配规则表。命中返回规则答案 dict；未命中/问句无数值返回 None。

    命中裁决（批次3 修订，防患于未然——覆盖部分重叠区间场景）：
    1. 命中集按来源文档分组：最新文档的规则覆盖问句数值时生效——同区间冲突
       （"5000 元以上"两版审批人不同）与**部分重叠冲突**（旧"3000 至 8000 元"
       vs 新"5000 元以上"，重叠段 [5000,8000] 归新文档）同一语义统一处理；
       文档分组按 _rule_doc_key 降序（effective_date 优先，无则回退
       created_at——"新制度覆盖旧制度"按版本日期）
    2. 同文档内多条命中（相邻区间边界，如"1000 元以内"与"1000 至 5000 元"
       在 1000 边界重叠）→ min_value 更大者优先（质检 R2 教训：不依赖行序）"""
    business = next((b for b in _BUSINESS_TYPES if b in question), None)
    unit, value = extract_query_value(question)
    if business is None or value is None:
        return None
    # 命中集按文档分组（doc_key=裁决时间键）
    hits_by_doc: dict[datetime, list[Rule]] = {}
    for rule in rules:
        if rule.business_type != business or rule.unit != unit:
            continue
        low_ok = rule.min_value is None or value >= rule.min_value
        high_ok = rule.max_value is None or value <= rule.max_value
        if low_ok and high_ok:
            hits_by_doc.setdefault(_rule_doc_key(rule), []).append(rule)
    if not hits_by_doc:
        return None
    # 最新文档的命中规则中，取 min_value 更大者（同文档边界归属更高档）。
    # 批次F 前置小批：min_value 平局时按 approver 字典序定序——同文档
    # 同区间多条规则多为等价重复（"人事部备案"/"报人事部备案"），此前
    # max 平局取首条依赖查询序（UUID 主键序）不确定；定序只为消除
    # 随机性，不做"哪条更权威"的语义承诺（真矛盾由 find_conflicts
    # 检测报警）
    newest_key = max(hits_by_doc.keys())
    cands = hits_by_doc[newest_key]
    hit = max(cands, key=lambda r: (r.min_value or 0, r.approver))
    # 批次F 验收修复：审批链叠加语义——制度原文"三天以上由部门负责人
    # 审批，七天以上报人事部备案"是两步叠加（审批+备案），不是二选一；
    # 此前只返回最高档一条（漏掉审批环节——真实验收："请假 10 天"
    # 只答备案）。按动作分组各取最高档，输出完整链（按档位降序）；
    # 主项 approver/action 保持最高档不变（评估口径向后兼容）
    chain = []
    for action_kind in ("审批", "审核", "批准", "批复", "备案", "报备"):
        group = [r for r in cands if r.action == action_kind]
        if group:
            chain.append(max(group, key=lambda r: (r.min_value or 0, r.approver)))
    chain.sort(key=lambda r: -(r.min_value or 0))
    return {
        "business_type": hit.business_type,
        "value": value,
        "unit": unit,
        "approver": hit.approver,
        "action": hit.action,
        "rule_text": format_rule_text(hit),
        "chain": [{"approver": r.approver, "action": r.action,
                   "rule_text": format_rule_text(r)} for r in chain],
    }


def _as_aware(dt: datetime | None) -> datetime:
    """时间统一为 UTC aware（naive 假定 UTC 附加时区）——
    防 naive/aware 混合比较崩溃（Document.created_at 读出来是 naive、
    effective_date 是 aware，直接比较 TypeError）"""
    if dt is None:
        return _EPOCH
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _rule_doc_key(rule: Rule) -> datetime:
    """规则来源文档的裁决时间键：effective_date 优先——"新制度覆盖旧制度"
    按版本日期不按归档上传时间；无生效日期（"自发布之日起生效"句式）
    回退 created_at——现行制度无日期时按上传时间参与比较，不是最旧。
    同一文档的规则键相同（effective/created 都是文档级属性）。
    注意：不同文档的键也可能相同（同样无日期、同上传批次）——此时
    并入同组、跨文档由 min_value 裁决，行为确定（建议4 补注）"""
    return _as_aware(rule.effective_date or rule.created_at)


def format_rule_text(rule: Rule) -> str:
    """规则 → 可读文本（供 [规则] 引用展示与 DB 来源字段共用；
    质检 R5：此前 matcher/service 各写一份，已合并）"""
    num = lambda v: f"{v:g}"
    if rule.min_value is None or rule.min_value == 0:
        rng = f"{num(rule.max_value)}{rule.unit}以内" if rule.max_value else "全部"
    elif rule.max_value is None:
        rng = f"{num(rule.min_value)}{rule.unit}以上"
    else:
        rng = f"{num(rule.min_value)} 至 {num(rule.max_value)}{rule.unit}"
    return f"{rule.business_type}{rng}：{rule.approver}{rule.action}"
