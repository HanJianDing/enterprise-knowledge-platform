"""审批链规则解析器（批次2-4 规则腿）——纯正则解析，零 LLM 成本

从审批类文档文本中提取规则三元组：
  "报销金额 1000 至 5000 元：部门负责人审批"
  → {business_type: 报销, min_value: 1000.0, max_value: 5000.0,
     unit: 元, approver: 部门负责人, action: 审批}

句式覆盖面（企业制度文本常见变体）：
- 区间：X 元以内 / X 元以上 / X 至 Y 元 / X（万）元以上 / 满 X 元
- 天数：X 天及以上 / X 天以上
- 动作：审批 / 审核 / 批准 / 备案 / 报备
- 分隔符：：/ :  ；/ ;  。

设计约束：解析不了就跳过（宁缺勿滥）——规则腿答不了的问题
静默回退向量检索，绝不产出猜测性规则。
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone

# 业务类型关键词（顺序即优先级，长词在前防"采购报销"类误吞）
_BUSINESS_TYPES = ["报销", "采购", "请假"]

# 区间模式（金额类）：
# 组1=下限数字 组2=下限单位(万/千/百，可空) 组3=区间类型(以内/以上/至/及以上/以下)
# 组4=上限数字(至) 组5=上限单位(可空)
# 注意：下限的"元"可选——制度文本"1000 至 5000 元"的单位只在末尾出现；
# 批次3.5 覆盖面：数字支持千分位（"5,000 元"）、区间类型前支持括注
# （"10 万（含）至 50 万"、"5000 元（含）以上"——（含）不改变区间语义，忽略）
_NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?"
_AMOUNT_RANGE = re.compile(
    r"(" + _NUM + r")\s*(万|千|百)?元?(?:（含）|\(含\))?\s*"
    r"(以内|以上|以下|及以上|至\s*(" + _NUM + r")\s*(万|千|百)?元(?:（含）)?)"
)
# 天数模式（请假类）：阿拉伯/中文数字（"三天及以上"）
_CN_DIGITS = "一二两三四五六七八九十"
_DAYS_RANGE = re.compile(
    r"(\d+(?:\.\d+)?|[" + _CN_DIGITS + r"]+)\s*天(?:（含）|\(含\))?\s*(以内|以上|以下|及以上)"
)
# 全角数字归一化表（"５０００元"→"5000元"）
_FULLWIDTH_DIGITS = str.maketrans(
    "０１２３４５６７８９，", "0123456789,"
)


def _num_to_float(raw: str) -> float:
    """数字文本 → 浮点（含千分位"5,000"→5000.0）"""
    return float(raw.replace(",", ""))
# 审批主体+动作（区间尾部锚定版）——从区间匹配结束位置起找主体，
# 批次11a 句式扩充：新语料（批次4 补形态后）无冒号写法——
# "单笔 500 元以内行政经理审批" / "1 万元以内 IT 经理审批" /
# "由直属上级审批" / "：部门负责人审批"（冒号/引导词均可，主体前
# 不允许数字——防"1000 元以内"残留被吞成主体前缀）。
# 质检 P2 修复：①主体前负向断言拦"无需/不用"类否定句（"500 元以内
# 无需审批"不得产出 approver="无需"的方向性错误）②引导词分支允许
# "且/并"连词（"且需部门经理审批"不得吞成"且需部门经理"）
_APPROVER_TAIL = re.compile(
    r"^\s*(?:且|并)?\s*(?:由|经|需|须)?\s*[：:]?\s*"
    r"(?!无[需须]|不[需用]|免[于除]|免\s*审批|除)"
    r"([^；;。，,：:0-9且并]{2,30}?)\s*(审批|审核|批准|批复|备案|报备)"
)
# 主体前置句式（区间在前的主体未命中时回查）："部门负责人审批 1000 元以内"——
# 区间前的文本以"主体+动作"结尾；同样带否定断言（批次语料增强二期加
# "除"拦截——"除前述审批"是指代句式不是主体，且 picture 合成文本的
# OCR 截断会产生"除前述"残句，抽成 approver 是方向性错误）
_APPROVER_BEFORE = re.compile(
    r"(?!无[需须]|不[需用]|免[于除]|免\s*审批|除)"
    r"([^；;。，,：:0-9且并]{2,30}?)\s*(审批|审核|批准|批复|备案|报备)\s*$"
)
# 除外从句（批次F 前置小批 T1 + P3-2 段级化）："除总经理外均由部门
# 经理审批"——剥离"除 X 外/以外（，）（均/都）"后，剩余部分的主体才是
# 真审批人。在 parse_text 段落级执行（seg 切分之前）——逗号既是 seg
# 分隔符又是除外从句分隔符，tail 级剥离够不着逗号后的主体（实测：
# "除总经理外，均由…"被逗号切成两段后 0 条）。
# 与 _APPROVER_TAIL 的"除"拦截分工：含"外"的除外从句剥掉取主体，
# 不含"外"的指代残句（"除前述审批"）剥离不适用、保持拦截。
_EXCLUDE_CLAUSE = re.compile(
    r"除[^外]{1,20}(?:外|以外)[，,]?\s*(?:均|都)?\s*"
)


# 引导前缀规范化：段级剥离除外从句后 tail 常带"：由/由"组合——引导词
# 与冒号的顺序不固定（"：由部门经理审批"/"由部门经理审批"），统一剥到
# 主体开头再交给 _APPROVER_TAIL（其内部可选引导词只支持"由在冒号前"
# 一种顺序，冒号在前的"：由"会把"由"漏进捕获组、被"由"守卫误拦）
_LEAD_IN = re.compile(
    r"^\s*(?:且|并)?\s*(?:[：:]|由|经|需|须)?\s*(?:[：:]|由|经|需|须)?\s*"
)


def _extract_approver_tail(tail: str) -> tuple[str, str] | None:
    """区间尾部子串 → (approver, action)；除外从句已由段级剥离，
    此处剥引导前缀后匹配，并做守卫（质检 P2，宁缺勿滥）——主体组内
    出现"由"（内嵌"外"剥离过界的"捐赠外由总经理"类残留）、以顿号
    开头（多重除外并列被段级全剥后剩"、均…"分隔符残留；逗号版多重
    除外逗号已被从句正则吃掉，无此残留）任一命中即放弃本句，
    宁可 0 条也不产出方向性错误的垃圾规则。"""
    m = _APPROVER_TAIL.match(_LEAD_IN.sub("", tail))
    if m is None:
        return None
    approver = m.group(1).strip()
    if "由" in approver or approver.startswith(("、", "，")):
        return None
    return approver, m.group(2)
# 制度生效日期（批次3 冲突裁决依据）："自 2024 年 1 月 1 日起生效"
_EFFECTIVE_DATE = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日起生效"
)
# 单位换算（统一存元/天）
UNIT_FACTOR = {"": 1.0, "百": 100.0, "千": 1000.0, "万": 10000.0}


def extract_effective_date(text: str) -> datetime | None:
    """从制度文本抽生效日期；无"自 X 年 X 月 X 日起生效"句式返回 None
    （现行制度常用"自发布之日起生效"——无日期，裁决时回退上传时间）"""
    m = _EFFECTIVE_DATE.search(text)
    if m is None:
        return None
    try:
        # 带 UTC 时区（与 DB 的 aware datetime 一致，防 naive/aware 比较崩溃）
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class Rule:
    """一条审批链规则（数值区间闭区间语义；max=None 表示上不封顶）。
    effective_date=制度生效日期（文本"自 X 年 X 月 X 日起生效"解析）；
    created_at=来源文档上传时间。批次3 冲突裁决键：effective_date 优先
    （真实企业逻辑"新制度覆盖旧制度"按版本日期，不按归档上传时间）"""
    business_type: str
    min_value: float | None
    max_value: float | None
    unit: str  # 元 / 天
    approver: str
    action: str
    effective_date: object | None = None  # datetime；文本无日期时为 None
    created_at: object | None = None  # datetime；解析器产物为 None，入库时带出


def _factor(unit: str | None) -> float:
    return UNIT_FACTOR.get(unit or "", 1.0)


def parse_text(text: str) -> list[Rule]:
    """解析整段文档文本 → 规则列表。无匹配返回空列表。

    结构认知（企业制度文本惯例）：段落（句号/换行分隔）内多个"区间：审批人"
    用分号/逗号并列，业务类型（报销/采购/请假）只出现在段落开头或标题——
    所以按段落切分继承业务类型，段内按分号/逗号切成"区间：审批主体动作"
    单元各自成对。批次11a：新语料用逗号并列（"500 元以内行政经理审批，
    500 元以上总经理审批"），分号切分扩展到逗号。
    """
    # 全角数字/逗号归一化（"５０００元"→"5000元"）
    text = text.translate(_FULLWIDTH_DIGITS)
    rules: list[Rule] = []
    # 生效日期是文档级属性（尾部"本制度自 X 年 X 月 X 日起生效"），全文抽一次
    effective_date = extract_effective_date(text)
    business: str | None = None
    for raw_para in re.split(r"[。\n]", text):
        para = raw_para.strip()
        if not para:
            continue
        # 业务类型出现在本段时更新（继承到后续分号/逗号单元）；
        # 必须在剥离之前扫描——业务词仅出现在除外从句内时（"除报销
        # 外…"）剥离后扫描会丢业务类型（质检 P3-4）
        for b in _BUSINESS_TYPES:
            if b in para:
                business = b
                break
        # 除外从句段级剥离（批次F 前置小批 P3-2）："除X外/以外（，）
        # （均/都）"剥掉后主体才是真审批人——必须在 seg 切分之前剥，
        # 逗号是 seg 分隔符，段内剥完逗号问题自然消失
        para = _EXCLUDE_CLAUSE.sub("", para)
        if business is None:
            continue
        # 段内按分号/逗号切成"区间：审批主体动作"单元；
        # 逗号前后都是数字时是千分位（"5,000 元"），不切
        for raw_seg in re.split(r"[;；]|(?<!\d)[,，](?!\d)", para):
            seg = raw_seg.strip()
            if not seg:
                continue
            # 金额类区间（每个区间匹配后从区间尾部找审批主体——
            # 同单元多区间时各自成对，如"500 元以内 X 审批 500 元以上 Y 审批"）
            amount_matches = list(_AMOUNT_RANGE.finditer(seg))
            for m in amount_matches:
                low = _num_to_float(m.group(1)) * _factor(m.group(2))
                kind = m.group(3)
                # 对区间尾部子串匹配（match 的 pos 参数下 ^ 仍锚定字符串
                # 开头——Python 语义，子串才是"区间后"锚定）；
                # 含"除…外"除外从句的句式在 helper 内剥离后取真主体
                extracted = _extract_approver_tail(seg[m.end():])
                if extracted is not None:
                    approver, action = extracted
                else:
                    # 主体前置句式："部门负责人审批 1000 元以内"——区间前
                    # 文本以"主体+动作"结尾（批次11a 恢复旧版支持的句式）
                    before = seg[:m.start()].rstrip()
                    bm = _APPROVER_BEFORE.search(before)
                    if bm is None:
                        continue
                    approver = bm.group(1).strip()
                    action = bm.group(2)
                # 否定句拦截（质检 P2）："无需/不用/免于"开头的"主体"是
                # 否定语义，不是规则（宁缺勿滥）。批次语料增强二期加"除"：
                # "除前述审批"是指代句式（OCR 截断残句），不是审批主体
                if approver.startswith(("无", "不", "免", "除")):
                    continue
                if kind in ("以内", "以下"):
                    rules.append(Rule(business, 0.0, low, "元", approver, action, effective_date))
                elif kind in ("以上", "及以上"):
                    rules.append(Rule(business, low, None, "元", approver, action, effective_date))
                else:  # 至：区间 [low, high]
                    high = _num_to_float(m.group(4)) * _factor(m.group(5))
                    rules.append(Rule(business, low, high, "元", approver, action, effective_date))
            # 天数类区间（本单元无金额匹配时才查，防混句）
            if not amount_matches:
                for m in _DAYS_RANGE.finditer(seg):
                    days = _cn_to_float(m.group(1))
                    if days is None:
                        continue
                    kind = m.group(2)
                    extracted = _extract_approver_tail(seg[m.end():])
                    if extracted is not None:
                        approver, action = extracted
                    else:
                        before = seg[:m.start()].rstrip()
                        bm = _APPROVER_BEFORE.search(before)
                        if bm is None:
                            continue
                        approver = bm.group(1).strip()
                        action = bm.group(2)
                    if approver.startswith(("无", "不", "免", "除")):
                        continue
                    if kind in ("以内", "以下"):
                        rules.append(Rule(business, 0.0, days, "天", approver, action, effective_date))
                    else:
                        rules.append(Rule(business, days, None, "天", approver, action, effective_date))
    return rules


def _cn_to_float(raw: str) -> float | None:
    """中文数字 → 数值：支持"三"=3、"十"=10、"十五"=15、"二十三"=23
    （逐字累加，遇"十"进位；解析不了返回 None 宁缺勿滥）"""
    if raw.isdigit():
        return float(raw)
    total = 0
    for ch in raw:
        if ch in "一二两三四五六七八九":
            total += {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                      "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}[ch]
        elif ch == "十":
            total = 10 if total == 0 else total * 10
        else:
            return None
    return float(total) if total else None
