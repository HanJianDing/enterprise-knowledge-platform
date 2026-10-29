"""批次2-4 规则腿测试：审批链规则解析器 / 问句匹配器 / 评估打分"""
from datetime import datetime, timezone

import pytest

from app.rag.rules.matcher import extract_query_value, match
from app.rag.rules.parser import Rule, parse_text

APPROVAL_TEXT = """云帆科技审批权限与流程制度

第一条 请假审批链
员工请假：直属上级审批；请假三天及以上：部门负责人审批；七天以上：人事部备案。

第二条 报销审批链
报销金额 1000 元以内：直属上级审批；1000 至 5000 元：部门负责人审批；5000 元以上：财务总监审批。

第三条 采购审批链
采购金额 5 万元以内：部门负责人审批；5 万元以上：总经理审批。
"""


class TestParser:
    def test_parses_amount_ranges(self):
        rules = parse_text(APPROVAL_TEXT)
        baoxiao = [r for r in rules if r.business_type == "报销"]
        assert len(baoxiao) == 3
        assert (baoxiao[0].min_value, baoxiao[0].max_value) == (0.0, 1000.0)
        assert baoxiao[0].approver == "直属上级"
        assert (baoxiao[1].min_value, baoxiao[1].max_value) == (1000.0, 5000.0)
        assert baoxiao[1].approver == "部门负责人"
        assert (baoxiao[2].min_value, baoxiao[2].max_value) == (5000.0, None)
        assert baoxiao[2].approver == "财务总监"

    def test_parses_wan_unit(self):
        """5 万元 → 50000 元（统一换算存储）"""
        rules = parse_text(APPROVAL_TEXT)
        caigou = [r for r in rules if r.business_type == "采购"]
        assert (caigou[0].min_value, caigou[0].max_value) == (0.0, 50000.0)
        assert caigou[1].min_value == 50000.0

    def test_parses_days_ranges(self):
        rules = parse_text(APPROVAL_TEXT)
        qingjia = [r for r in rules if r.business_type == "请假"]
        assert len(qingjia) == 2
        assert (qingjia[0].min_value, qingjia[0].max_value) == (3.0, None)
        assert qingjia[0].action == "审批"
        assert qingjia[1].min_value == 7.0
        assert qingjia[1].action == "备案"
        assert qingjia[1].approver == "人事部"

    def test_non_rule_text_skipped(self):
        """无"：XX审批"结构的句子不产出规则（宁缺勿滥）"""
        rules = parse_text("员工应当遵守考勤制度。报销流程详见财务部公告。")
        assert rules == []

    def test_no_approver_structure_skipped(self):
        """有金额但无审批主体结构 → 跳过"""
        rules = parse_text("报销金额标准为每天 200 元。")
        assert rules == []

    def test_pronoun_approver_skipped(self):
        """批次语料增强二期回归钉住："除前述审批"是指代句式不是审批主体
        （请假审批流程图.png 的 OCR 截断残句曾被抽成 approver="除前述"
        的坏规则，压过现行制度——真实事故）"""
        rules = parse_text("请假七天以上：除前述审批")
        assert rules == []

    def test_pronoun_approver_skipped_tail_variant(self):
        """含"外"的除外从句走剥离（批次F 前置小批 T1 升级）："除前述
        情形外由总经理审批"的主体是总经理——剥离从句后正确产出"""
        rules = parse_text("报销 5000 元以上：除前述情形外由总经理审批")
        assert len(rules) == 1
        assert rules[0].approver == "总经理"

    def test_exclusion_clause_stripped(self):
        """批次F 前置小批 T1：除外从句剥离——"除总经理外均由部门经理
        审批"的正确主体是部门经理（不是整条丢弃、也不是把除外从句
        当主体），正确主体的规则产出"""
        rules = parse_text("员工请假 500 元以内：除总经理外均由部门经理审批")
        assert len(rules) == 1
        assert rules[0].approver == "部门经理"
        assert rules[0].action == "审批"
        assert (rules[0].min_value, rules[0].max_value) == (0.0, 500.0)

    def test_exclusion_clause_stripped_variant(self):
        """"除…以外"变体 + 无"均/都"副词同样剥离"""
        rules = parse_text("报销 1000 元以上：除董事长以外财务总监审批")
        assert len(rules) == 1
        assert rules[0].approver == "财务总监"

    def test_exclusion_strip_guard_embedded_wai(self):
        """质检 P2 保守回归①：X 内嵌"外"（"除对外捐赠外…"）剥离会过界
        产出"捐赠外由总经理"垃圾主体——守卫命中，放弃本句（宁缺勿滥）"""
        rules = parse_text("报销 5000 元以上：除对外捐赠外由总经理审批")
        assert rules == []

    def test_exclusion_strip_guard_multiple_exclusions(self):
        """质检 P2 保守回归②：多重除外并列（"除A外、除B外…"）——段级
        剥离把两个从句都剥掉后剩顿号开头的残句，顿号守卫放弃本句"""
        rules = parse_text("报销 5000 元以上：除总经理外、除财务总监外均由部门经理审批")
        assert rules == []

    def test_exclusion_strip_guard_dunhao_variant(self):
        """质检 P2 保守回归③：顿号变体（"除A外、均由…"）——剥离过界
        残留顿号开头，放弃本句"""
        rules = parse_text("报销 5000 元以上：除总经理外、均由部门经理审批")
        assert rules == []

    def test_exclusion_fullwidth_comma(self):
        """留档 P3-2 修复回归：全角逗号分隔的除外从句（"除A外，由B
        审批"）——逗号是 seg 分隔符，段级剥离后才能抽出逗号后的主体"""
        rules = parse_text("报销 5000 元以上：除总经理外，均由部门经理审批")
        assert len(rules) == 1
        assert rules[0].approver == "部门经理"

    def test_business_word_inside_exclusion_clause(self):
        """质检 P3-4 修复回归：业务词仅出现在除外从句内时（"除报销
        相关情形外…"），剥离前扫描业务类型，规则照常抽出"""
        rules = parse_text("除报销相关情形外 5000 元以上由财务总监审批")
        assert len(rules) == 1
        assert rules[0].business_type == "报销"
        assert rules[0].approver == "财务总监"


class TestMatcher:
    def _rules(self):
        return parse_text(APPROVAL_TEXT)

    def test_extract_amount(self):
        assert extract_query_value("报销 8000 元谁审批？") == ("元", 8000.0)
        assert extract_query_value("采购 30 万元由谁审批？") == ("元", 300000.0)
        assert extract_query_value("报销 500 元找谁审批？") == ("元", 500.0)
        assert extract_query_value("请假 10 天怎么办理？") == ("天", 10.0)
        assert extract_query_value("请假十天怎么办理？") == ("天", 10.0)
        # 质检 R5/R6 修复锁定：组合中文数字（"二十三"=23 而非 30）
        assert extract_query_value("请假二十三天怎么办理？") == ("天", 23.0)
        assert extract_query_value("请假十三天怎么办理？") == ("天", 13.0)

    def test_match_amount_ranges(self):
        rules = self._rules()
        assert match(rules, "报销 3000 元谁审批？")["approver"] == "部门负责人"
        assert match(rules, "报销 8000 元谁审批？")["approver"] == "财务总监"
        assert match(rules, "报销 500 元找谁审批？")["approver"] == "直属上级"
        assert match(rules, "采购 30 万元由谁审批？")["approver"] == "总经理"
        assert match(rules, "采购 2 万元谁审批？")["approver"] == "部门负责人"

    def test_match_days_ranges(self):
        rules = self._rules()
        assert match(rules, "请假 5 天谁审批？")["approver"] == "部门负责人"
        hit = match(rules, "请假 10 天怎么办理？")
        assert hit["approver"] == "人事部"
        assert hit["action"] == "备案"
        # 批次F 验收修复：审批链叠加语义——主项保持最高档（人事部备案，
        # 评估口径不变），chain 含完整两步（部门负责人审批+人事部备案）
        chain_pairs = {(s["approver"], s["action"]) for s in hit["chain"]}
        assert ("部门负责人", "审批") in chain_pairs
        assert ("人事部", "备案") in chain_pairs

    def test_match_single_step_chain(self):
        """报销 3000：无叠加动作——chain 只有单步（审批）"""
        hit = match(self._rules(), "报销 3000 元谁审批？")
        assert hit["approver"] == "部门负责人"
        assert len(hit["chain"]) == 1
        assert hit["chain"][0]["approver"] == "部门负责人"

    def test_no_value_returns_none(self):
        """问句无数值（泛问"报销怎么审批"）→ 不匹配，回退向量"""
        assert match(self._rules(), "报销怎么审批？") is None
        assert match(self._rules(), "年假怎么申请？") is None

    def test_out_of_range_returns_none(self):
        """请假 1 天：规则从 3 天起 → 不匹配（原文"员工请假：直属上级审批"
        无数值区间，解析器不产出 0~3 天规则——制度原文如此，回退向量）"""
        assert match(self._rules(), "请假 1 天谁审批？") is None

    def test_same_range_tie_breaks_deterministically(self):
        """批次F 前置小批：同文档同区间平局按 approver 定序（Unicode
        码点序，本例"财务总监" > "总经理"）——消除 UUID 主键查询序依赖
        （此前平局结果随机）；无论列表顺序结果确定"""
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rules = [
            Rule("报销", 5000.0, None, "元", "总经理", "审批", now, now),
            Rule("报销", 5000.0, None, "元", "财务总监", "审批", now, now),
        ]
        assert match(rules, "报销 8000 元谁审批？")["approver"] == "财务总监"
        assert match(list(reversed(rules)), "报销 8000 元谁审批？")["approver"] == "财务总监"

    def test_conflict_resolved_by_effective_date(self):
        """批次3 冲突裁决：旧制度（effective 2024-01-01，但今天才归档上传）
        不得赢过现行制度（无生效日期句式，回退上传时间）——
        "新制度覆盖旧制度"按版本日期不按归档时间"""
        from datetime import datetime

        old_rule = Rule("报销", 5000.0, None, "元", "总经理", "审批",
                        effective_date=datetime(2024, 1, 1),
                        created_at=datetime(2026, 10, 8))  # 后归档
        current_rule = Rule("报销", 5000.0, None, "元", "财务总监", "审批",
                            effective_date=None,
                            created_at=datetime(2026, 9, 24))  # 先上传的现行版
        hit = match([old_rule, current_rule], "报销 8000 元谁审批？")
        assert hit["approver"] == "财务总监"

    def test_conflict_resolved_by_effective_date_when_both_have(self):
        """两版都有生效日期 → 直接按日期裁决"""
        from datetime import datetime

        v2024 = Rule("采购", 50000.0, None, "元", "财务总监", "审批",
                     effective_date=datetime(2024, 1, 1))
        v2026 = Rule("采购", 50000.0, None, "元", "总经理", "审批",
                     effective_date=datetime(2026, 1, 1))
        hit = match([v2024, v2026], "采购 30 万元由谁审批？")
        assert hit["approver"] == "总经理"

    def test_partial_overlap_segment_ruling(self):
        """部分重叠分段裁决（防患于未然）：旧 [3000,8000] 部门负责人、
        新 [5000,∞) 财务总监——
        4000（仅旧段）→ 部门负责人；6000（重叠段）→ 新制度财务总监；
        9000（仅新段）→ 财务总监"""
        old = Rule("报销", 3000.0, 8000.0, "元", "部门负责人", "审批",
                   effective_date=datetime(2024, 1, 1, tzinfo=timezone.utc))
        new = Rule("报销", 5000.0, None, "元", "财务总监", "审批",
                   effective_date=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert match([old, new], "报销 4000 元谁审批？")["approver"] == "部门负责人"
        assert match([old, new], "报销 6000 元谁审批？")["approver"] == "财务总监"
        assert match([old, new], "报销 9000 元谁审批？")["approver"] == "财务总监"

    def test_threshold_shift_conflict_flagged(self):
        """门槛平移+审批人变更（质检3.5 盲区）：旧"3000 以上：部门负责人审批"
        vs 新"5000 以上：财务总监审批"——包含关系但同动作不同审批人=真冲突，
        管理端必须告警（匹配裁决本身正确：最新文档覆盖）"""
        from datetime import datetime, timezone

        old = Rule("报销", 3000.0, None, "元", "部门负责人", "审批",
                   effective_date=datetime(2024, 1, 1, tzinfo=timezone.utc))
        new = Rule("报销", 5000.0, None, "元", "财务总监", "审批",
                   effective_date=datetime(2026, 1, 1, tzinfo=timezone.utc))
        # 匹配裁决：重叠段 [5000,∞) 归新文档
        assert match([old, new], "报销 8000 元谁审批？")["approver"] == "财务总监"
        assert match([old, new], "报销 4000 元谁审批？")["approver"] == "部门负责人"

    def test_ladder_design_not_flagged_as_conflict(self):
        """阶梯设计（动作不同）不报冲突：请假"三天及以上：部门负责人审批"
        vs"七天以上：人事部备案"——子档更精确且动作不同，是制度设计"""
        from datetime import datetime, timezone

        rule_a = Rule("请假", 3.0, None, "天", "部门负责人", "审批",
                      effective_date=datetime(2024, 1, 1, tzinfo=timezone.utc))
        rule_b = Rule("请假", 7.0, None, "天", "人事部", "备案",
                      effective_date=datetime(2026, 1, 1, tzinfo=timezone.utc))
        # 匹配裁决不受影响（7 天以上 min 更大者优先）
        assert match([rule_a, rule_b], "请假 10 天怎么办理？")["approver"] == "人事部"

    def test_new_syntax_coverage(self):
        """句式覆盖面（批次3.5）：千分位、括注、全角数字"""
        text = ("报销金额 5,000 元（含）以上：总经理审批；"
                "10 万（含）至 50 万元：财务总监审批。")
        rules = parse_text(text)
        assert len(rules) == 2
        assert rules[0].min_value == 5000.0 and rules[0].approver == "总经理"
        assert (rules[1].min_value, rules[1].max_value) == (100000.0, 500000.0)

        fullwidth = parse_text("报销金额５０００元以上：总经理审批。")
        assert fullwidth[0].min_value == 5000.0

    def test_no_colon_syntax(self):
        """句式覆盖面（批次11a）：无冒号+逗号并列（批次4 语料补形态后的写法）——
        「单笔 500 元以内行政经理审批，500 元以上总经理审批」/「由 X 审批」"""
        text = ("行政采购方面：单笔 500 元以内行政经理审批，"
                "500 元以上总经理审批。")
        rules = parse_text(text)
        assert len(rules) == 2
        assert (rules[0].min_value, rules[0].max_value, rules[0].approver) == (
            0.0, 500.0, "行政经理")
        assert rules[1].min_value == 500.0 and rules[1].approver == "总经理"

        you_text = ("报销金额 1000 元以内由直属上级审批，"
                    "1000 至 5000 元由部门负责人审批，"
                    "5000 元以上由财务总监审批。")
        you_rules = parse_text(you_text)
        assert len(you_rules) == 3
        assert you_rules[0].approver == "直属上级"
        assert you_rules[1].approver == "部门负责人"
        assert you_rules[2].approver == "财务总监"

    def test_it_prefix_approver(self):
        """句式覆盖面（批次11a）：IT 经理等英文主体（无引导词直接主体）"""
        rules = parse_text("IT 设备采购 1 万元以内 IT 经理审批，1 万元以上总经理审批。")
        assert len(rules) == 2
        assert rules[0].approver == "IT 经理"
        assert rules[0].max_value == 10000.0
        assert rules[1].approver == "总经理"

    def test_negative_approval_not_parsed(self):
        """质检 P2 回归钉住："无需/不用"否定句不得产出方向性错误规则"""
        assert parse_text("报销金额 500 元以内无需审批。") == []
        assert parse_text("采购 1000 元以内不用备案。") == []
        assert parse_text("请假一天以内免于报备。") == []

    def test_conjunction_prefix_not_swallowed(self):
        """质检 P2 回归钉住："且需部门经理审批"不得吞成 approver="且需部门经理" """
        rules = parse_text("报销金额 2000 元以上且需部门经理审批。")
        assert len(rules) == 1
        assert rules[0].approver == "部门经理"

    def test_approver_before_range(self):
        """主体前置句式（批次11a 恢复）："部门负责人审批 1000 元以内" """
        rules = parse_text("报销：部门负责人审批 1000 元以内，财务总监审批 1000 元以上。")
        assert len(rules) == 2
        assert rules[0].approver == "部门负责人"
        assert rules[0].max_value == 1000.0
        assert rules[1].approver == "财务总监"


class TestApprovalEval:
    @pytest.mark.asyncio
    async def test_approval_metrics_computed(self, monkeypatch):
        from app.rag.rules import service as rules_service
        from app.services import eval_service

        async def fake_match(question):
            if "3000" in question:
                return {"approver": "部门负责人", "action": "审批",
                        "business_type": "报销", "value": 3000.0, "unit": "元",
                        "rule_text": "报销1000 至 5000元：部门负责人审批"}
            return None

        monkeypatch.setattr(rules_service, "match_question", fake_match)
        queries = [
            {"question": "报销 3000 元谁审批？", "expected_approver": "部门负责人",
             "expected_action": "审批"},
            {"question": "报销 8000 元谁审批？", "expected_approver": "财务总监",
             "expected_action": "审批"},
        ]
        r = await eval_service._run_approval_group_async(queries)
        assert r["n"] == 2
        assert r["accuracy"] == 0.5
        assert r["details"][1]["got"] is None
