"""产品化 A1 测试：分块模板化（条款 / 问答对 / 表格 / 标题层级 + 检测回落）"""
import pytest

from app.rag.chunker import (
    chunk_laws,
    chunk_faq,
    chunk_long_text,
    chunk_table,
    _detect_law_structure,
    _detect_qa_pairs,
    _parse_headings,
)

LAW_DOC = """第五章 假期管理
第十二条 婚假
员工结婚可享受婚假 3 天，需凭结婚证提前一周申请。
第十三条 年假
员工转正满一年后可享受带薪年假 5 天，逐年递增 1 天，上限 15 天。
第十四条 病假
病假需提供医院证明，每月累计不超过 2 天的不扣工资。
"""


# ════════════════════════════════════════════════════
# 条款模板（laws）
# ════════════════════════════════════════════════════

class TestChunkLaws:
    def test_law_density_detection(self):
        """条款密度达标时识别为条款结构"""
        assert _detect_law_structure(LAW_DOC) is True

    def test_law_density_low_false(self):
        """普通文本（无条款编号）不误判"""
        assert _detect_law_structure("这是普通说明文本。" * 50) is False

    def test_law_density_sparse_false(self):
        """条款稀疏（平均每 500 字不足一条）不误判"""
        sparse = "普通内容" * 300 + "\n第一条 附则\n" + "普通内容" * 300
        assert _detect_law_structure(sparse) is False

    def test_law_chunks_with_section_path(self):
        """每个块带章节路径前缀，chunk_type=laws"""
        chunks = chunk_laws(LAW_DOC, {"document_id": "d1"})
        assert len(chunks) >= 1
        joined = "\n".join(c.page_content for c in chunks)
        assert "第五章 假期管理" in joined
        assert "第十二条 婚假" in joined
        assert "婚假 3 天" in joined
        assert all(c.metadata["chunk_type"] == "laws" for c in chunks)
        assert any("第五章" in c.metadata["section_path"] for c in chunks)

    def test_law_small_items_merged(self):
        """多条小条款合并成块，不产生碎片"""
        chunks = chunk_laws(LAW_DOC, {"document_id": "d1"})
        # 三条条款都很短，应合并为 1 块而非 3 块
        assert len(chunks) == 1
        assert "第十四条 病假" in chunks[0].page_content

    def test_law_long_item_split_with_prefix(self):
        """超长条款被递归切分，每块保留条款路径前缀"""
        long_item = "第十二条 详细说明\n" + "说明内容。" * 200
        chunks = chunk_laws(long_item, {"document_id": "d1"})
        assert len(chunks) > 1
        for c in chunks:
            assert "【第十二条 详细说明】" in c.page_content

    def test_law_long_text_auto_detect(self):
        """long_text 自动检测条款结构并走条款模板"""
        chunks = chunk_long_text(LAW_DOC, {"document_id": "d1"})
        assert chunks[0].metadata["chunk_type"] == "laws"

    def test_law_preamble_keeps_section_path(self):
        """验收回归：文档开头有标题/前言时，章节路径不丢（曾整块丢失——acc_path
        只在累加区为空时设置，首个 path 为空的标题 section 会让后续合并永不更新路径）"""
        text = (
            "云帆科技考勤管理制度\n"
            "第一章 总则\n"
            "第一条 为规范考勤管理，特制定本制度。\n"
            "第二条 本制度适用于全体员工。\n"
            "第二章 工作时间\n"
            "第三条 每周五天工作制。\n"
        )
        chunks = chunk_laws(text, {"document_id": "d1"})
        paths = [c.metadata.get("section_path") or "" for c in chunks]
        # 验收批次E：跨章不合并——第一章与第二章各一块
        assert len(chunks) == 2
        assert "第一章 总则" in paths[0]
        assert "第二章 工作时间" in paths[1]
        assert "【" in chunks[0].page_content and "【" in chunks[1].page_content
        assert "第三条" in chunks[1].page_content  # 第二章内容在第二章块内
        assert "第三条" not in chunks[0].page_content

    def test_law_chapter_headings_kept_in_content(self):
        """验收批次E：章标题并入正文——旧版章标题只进路径不进正文，合并成一块时
        二~末章章名从索引中彻底消失（搜"第五章 加班管理"搜不到）"""
        text = (
            "云帆科技考勤管理制度\n"
            "第一章 总则\n"
            "第一条 为规范公司考勤管理，保障正常工作秩序，特制定本制度。\n"
            "第二章 工作时间\n"
            "第二条 每周五天工作制。\n"
            "第五章 加班管理\n"
            "第三条 加班餐费报销标准为每人每次 30 元，凭发票报销。\n"
        )
        chunks = chunk_laws(text, {"document_id": "d1"})
        joined = "\n".join(c.page_content for c in chunks)
        for heading in ("第一章 总则", "第二章 工作时间", "第五章 加班管理"):
            assert heading in joined, f"章标题 {heading} 应从正文中可搜到"

    def test_law_chunks_split_by_chapter(self):
        """验收批次E：按章分块——每块只含本章条款，块标签与内容一致；
        目标条款（第十四条 报销标准）住在自己的章块里，检索可精准命中"""
        text = (
            "云帆科技考勤管理制度\n"
            "第一章 总则\n"
            "第一条 为规范公司考勤管理，保障正常工作秩序，特制定本制度。\n"
            "第二条 本制度适用于公司全体员工（含试用期员工）。\n"
            "第三章 迟到与早退\n"
            "第五条 迟到 10 分钟以内，每次扣款 20 元。\n"
            "第六章 假期管理\n"
            "第十二条 年假 5 天。\n"
            "第五章 加班管理\n"
            "第十四条 加班餐费报销标准为每人每次 30 元，凭发票报销。\n"
        )
        chunks = chunk_laws(text, {"document_id": "d1"})
        # 4 个章 + 文档标题（并入首章块）→ 4 块
        assert len(chunks) == 4
        by_path = {c.metadata["section_path"]: c.page_content for c in chunks}
        chapter_heads = ["第一章 总则", "第三章 迟到与早退", "第六章 假期管理", "第五章 加班管理"]
        for head in chapter_heads:
            assert any(p.startswith(head) for p in by_path), f"缺章块: {head}"
        # 报销标准条款在自己的章块里，块标签为该章（不再标"第一章"装全篇）
        target = next(c for c in chunks if "第十四条" in c.page_content)
        assert "加班餐费报销标准为每人每次 30 元" in target.page_content
        assert target.metadata["section_path"].startswith("第五章 加班管理")
        assert "第一条" not in target.page_content  # 跨章内容不进本块

    def test_law_no_chapter_doc_merges_by_size(self):
        """验收批次E：无章结构的条款文档按尺寸合并，行为与旧版一致（不产生碎片）"""
        text = (
            "云帆科技2025年差旅费用标准\n"
            "第一条 本标准自 2025 年 1 月 1 日起生效，取代 2024 年版标准。\n"
            "第二条 住宿标准：一类城市（北上广深）450 元/晚，二类城市 400 元/晚。\n"
            "第三条 市内交通：一类城市每天 120 元，其余城市每天 100 元。\n"
            "第四条 餐费补助：每天 100 元，凭发票报销。\n"
        )
        chunks = chunk_laws(text, {"document_id": "d1"})
        assert len(chunks) == 1
        assert "第四条" in chunks[0].page_content
        assert chunks[0].metadata["section_path"].startswith("第一条")

    def test_law_merged_articles_keep_own_paths(self):
        """验收批次G：块内后续条款注入自己的路径行——旧合并只留块首路径，
        其余条款的路径被丢弃，块标签名不副实、来源展示无法定位条号"""
        text = (
            "云帆科技社保公积金管理制度\n"
            "第一条 参保范围\n"
            "公司为全体员工缴纳五险一金，入职当月参保。\n"
            "第二条 缴费基数\n"
            "社保公积金基数按员工上年度月平均工资确定。\n"
            "第三条 公积金比例\n"
            "住房公积金缴存比例为 12%。\n"
        )
        chunks = chunk_laws(text, {"document_id": "d1"})
        assert len(chunks) == 1  # 无章文档仍按尺寸合并成一块
        content = chunks[0].page_content
        # 块首条款由块前缀承载，不重复注入；其余条款各自带路径行
        assert content.count("【第一条 参保范围】") == 1
        assert "【第二条 缴费基数】" in content
        assert "【第三条 公积金比例】" in content
        assert chunks[0].metadata["section_path"] == "第一条 参保范围"


# ════════════════════════════════════════════════════
# 问答对模板（qa）
# ════════════════════════════════════════════════════

class TestChunkQA:
    def test_qa_pair_detection(self):
        """Q/A 标记成对解析，至少 2 对才生效；返回 (前言, 问答对)"""
        text = "问：VPN 连不上怎么办？\n答：先检查账号是否过期。\n问：WiFi 密码多少？\n答：见前台公告栏。\n"
        parsed = _detect_qa_pairs(text)
        assert parsed is not None
        preamble, pairs = parsed
        assert preamble == ""
        assert len(pairs) == 2
        assert "VPN" in pairs[0][0]

    def test_qa_single_pair_falls_back(self):
        """只有 1 对问答视为巧合，不结构化"""
        text = "问：VPN 连不上怎么办？\n答：先检查账号是否过期。\n"
        assert _detect_qa_pairs(text) is None

    def test_faq_preamble_kept_in_chunk(self):
        """验收批次E 回归：首问前的标题/前言不再丢弃——旧实现直接丢标题，
        主题名（常在标题里）从索引消失，按主题名检索搜不到该文档"""
        text = (
            "# 社保公积金常见问题（FAQ）\n"
            "问：公积金比例是多少？\n"
            "答：公司按 12% 缴纳。\n"
            "问：社保断缴怎么办？\n"
            "答：尽快补缴。\n"
        )
        chunks = chunk_faq(text, {"document_id": "d1"})
        joined = "\n".join(c.page_content for c in chunks)
        assert "# 社保公积金常见问题（FAQ）" in joined

    def test_faq_no_preamble_unchanged(self):
        """无前言的 FAQ 行为不变：问答对完整、不产生空块"""
        text = "问：A？\n答：一。\n问：B？\n答：二。\n"
        chunks = chunk_faq(text, {"document_id": "d1"})
        assert len(chunks) >= 1
        assert all(c.page_content.strip() for c in chunks)

    def test_faq_chunks_keep_pairs_intact(self):
        """一个问答对不被打散：问题与答案同块"""
        text = (
            "问：VPN 连不上怎么办？\n答：第一步检查账号，第二步检查网络。\n\n"
            "问：打印机怎么加？\n答：联系 IT 统一安装驱动。\n"
        )
        chunks = chunk_faq(text, {"document_id": "d1"})
        assert len(chunks) >= 1
        joined = "\n".join(c.page_content for c in chunks)
        assert "VPN 连不上怎么办" in joined
        assert "第二步检查网络" in joined
        assert all(c.metadata["chunk_type"] == "faq" for c in chunks)

    def test_faq_no_qa_markers_falls_back_to_old_behavior(self):
        """无问答标记时回落旧双换行切分（行为不劣化）"""
        text = "## 第一节：概述\n这是概述内容。\n\n## 第二节：详细说明\n这是详细说明内容。\n"
        chunks = chunk_faq(text, {"document_id": "d1"})
        assert len(chunks) == 1
        assert chunks[0].metadata["chunk_type"] == "faq"

    def test_qa_long_pair_split_with_question_prefix(self):
        """超长问答对被切分，问题作为每块前缀"""
        text = "问：报销流程是什么？\n答：" + "第一步到第九十九步。\n" * 120
        chunks = chunk_faq(text, {"document_id": "d1"})
        assert len(chunks) >= 1
        assert any("报销流程是什么" in c.page_content for c in chunks)


# ════════════════════════════════════════════════════
# 标题层级（long_text 升级）
# ════════════════════════════════════════════════════

class TestChunkHeadings:
    def test_markdown_headings_parsed(self):
        """Markdown 标题分段，少于 2 个标题视为无结构"""
        text = "# 第一章 总则\n内容一。\n## 第二章 细则\n内容二。\n"
        headings = _parse_headings(text)
        assert headings is not None
        assert headings[0][0] == "第一章 总则"
        assert headings[1][0] == "第一章 总则 → 第二章 细则"

    def test_chinese_headings_parsed(self):
        """中文一、/（一）编号分段"""
        text = "一、考勤要求\n每天九点打卡。\n（一）迟到处理\n迟到十分钟内不扣款。\n"
        headings = _parse_headings(text)
        assert headings is not None
        assert headings[0][0] == "一、考勤要求"

    def test_long_text_chunks_prefixed_with_heading(self):
        """长文本分块注入标题路径前缀"""
        text = "# 报销制度\n报销流程第一步到第十步。\n" + "## 差旅标准\n差旅内容。" * 30
        chunks = chunk_long_text(text, {"document_id": "d1"})
        assert len(chunks) >= 1
        assert any("【报销制度" in c.page_content for c in chunks)
        assert any(c.metadata.get("section_path") for c in chunks)

    def test_long_text_without_headings_unchanged(self):
        """无标题结构：开关默认关 → 行为与旧版一致（long_text 块、无 section_path）"""
        text = "这是一段很长的文本。" * 200
        chunks = chunk_long_text(text, {"document_id": "d1"})
        assert len(chunks) >= 1
        assert all(c.metadata["chunk_type"] == "long_text" for c in chunks)
        assert all(not c.metadata.get("section_path") for c in chunks)

    def test_parent_child_subchunking(self, monkeypatch):
        """验收批次1·1c（开关开启）：父块 ≤300 字原样保留；超长父块切子块——
        子块拼接覆盖父块全文、同父块子块 parent_id 一致"""
        monkeypatch.setattr("app.config.settings.retrieval_subchunk_enabled", True)
        monkeypatch.setattr("app.config.settings.retrieval_subchunk_min_chars", 0)
        short = "短段落。" * 10  # 40 字
        chunks = chunk_long_text(short, {"document_id": "d1"})
        assert len(chunks) == 1
        assert chunks[0].metadata["chunk_type"] == "long_text"  # 小父块不拆
        assert "parent_content" not in chunks[0].metadata

        long_text = "第一句很长内容。" * 60  # 480 字 → 拆子块
        chunks = chunk_long_text(long_text, {"document_id": "d2"})
        assert len(chunks) >= 2
        assert all(c.metadata["chunk_type"] == "sub_chunk" for c in chunks)
        # 子块拼接 = 父块全文（句边界切分不丢内容）
        joined = "".join(c.page_content for c in chunks)
        assert joined.replace(" ", "") == long_text.replace(" ", "")
        # 同一父块的子块共享 parent_id
        pids = {c.metadata["parent_id"] for c in chunks}
        assert len(pids) == 1


# ════════════════════════════════════════════════════
# 表格模板（table）
# ════════════════════════════════════════════════════

class TestChunkTable:
    def _sheets(self, n_rows: int = 45):
        headers = ["项目", "限额", "说明"]
        rows = [[f"项目{i}", f"{i * 10}元", f"说明{i}"] for i in range(1, n_rows + 1)]
        return [{"name": "报销标准", "headers": headers, "rows": rows}]

    def test_table_chunks_grouped_by_rows_with_header(self):
        """按行组切分（20 行/块），每块注入列头"""
        chunks = chunk_table(self._sheets(45), {"document_id": "d1"})
        # 45 行 → 20 + 20 + 5 = 3 块
        assert len(chunks) == 3
        for c in chunks:
            assert c.metadata["chunk_type"] == "table"
            assert c.metadata["table_header"] == "项目、限额、说明"
            assert "【表格：报销标准】列：项目、限额、说明" in c.page_content
        assert "行1：" in chunks[0].page_content
        assert "行45：" in chunks[2].page_content
        # 行不跨块重复
        assert "行20：" in chunks[0].page_content
        assert "行21：" in chunks[1].page_content

    def test_table_multi_sheets(self):
        """多 sheet 各产出一组块"""
        sheets = [
            {"name": "Sheet1", "headers": ["A"], "rows": [["1"], ["2"]]},
            {"name": "Sheet2", "headers": ["B"], "rows": [["3"]]},
        ]
        chunks = chunk_table(sheets, {"document_id": "d1"})
        names = {c.metadata["sheet_name"] for c in chunks}
        assert names == {"Sheet1", "Sheet2"}

    def test_table_long_cell_truncated(self):
        """畸形表格超长单元格被截断（防嵌入 token 超限）"""
        sheets = [{
            "name": "长文",
            "headers": ["标题", "内容"],
            "rows": [["正常", "很长的内容" * 500]],
        }]
        chunks = chunk_table(sheets, {"document_id": "d1"})
        assert len(chunks) == 1
        assert "…" in chunks[0].page_content
        assert len(chunks[0].page_content) < 800

    def test_table_empty(self):
        """空表返回空列表"""
        assert chunk_table([], {"document_id": "d1"}) == []
        assert chunk_table([{"name": "空", "headers": [], "rows": []}], {}) == []
