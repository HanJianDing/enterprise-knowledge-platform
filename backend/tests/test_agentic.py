"""产品化 B1 测试：Agentic RAG 编排（纯函数 + LLM mock，无真实调用）"""
import json

import pytest

from app.rag import agentic
from app.rag.agentic import (
    safe_eval_expression,
    merge_documents,
    decompose_question,
    extract_calculation,
    format_calculation_note,
)
from langchain_core.documents import Document


class FakeLLM:
    """返回固定 JSON 的假 LLM（invoke 返回带 .content 的对象）"""

    def __init__(self, payload: dict | str):
        self.payload = payload

    def invoke(self, prompt: str):
        class Resp:
            content = json.dumps(self.payload) if isinstance(self.payload, dict) else self.payload
        return Resp()


# ════════════════════════════════════════════════════
# safe_eval_expression — ast 白名单求值
# ════════════════════════════════════════════════════

class TestSafeEval:
    def test_basic_arithmetic(self):
        assert safe_eval_expression("10 - 3") == 7
        assert safe_eval_expression("8 * 2") == 16
        assert safe_eval_expression("(10 + 2) / 3") == 4.0

    def test_negative_and_decimal(self):
        assert safe_eval_expression("-5 + 2") == -3
        assert safe_eval_expression("2.5 * 4") == 10.0

    def test_division_by_zero_returns_none(self):
        assert safe_eval_expression("10 / 0") is None

    def test_injection_rejected(self):
        """函数调用/属性访问/名字访问全部拒绝（eval 注入面）"""
        assert safe_eval_expression("__import__('os').system('dir')") is None
        assert safe_eval_expression("open('/etc/passwd')") is None
        assert safe_eval_expression("[1,2,3][0]") is None
        assert safe_eval_expression("a + b") is None

    def test_empty_and_oversize(self):
        assert safe_eval_expression("") is None
        assert safe_eval_expression("1 + " * 40) is None


# ════════════════════════════════════════════════════
# merge_documents — 多路检索合并去重
# ════════════════════════════════════════════════════

class TestMergeDocuments:
    def _doc(self, doc_id: str, idx: int) -> Document:
        return Document(
            page_content=f"内容{doc_id}-{idx}",
            metadata={"document_id": doc_id, "chunk_index": idx},
        )

    def test_dedupe_keeps_order(self):
        a = self._doc("d1", 0)
        b = self._doc("d2", 0)
        a_dup = self._doc("d1", 0)
        c = self._doc("d1", 1)
        merged = merge_documents([[a, b], [a_dup, c]])
        assert len(merged) == 3
        assert merged[0].metadata["document_id"] == "d1"
        assert merged[1].metadata["document_id"] == "d2"
        assert merged[2].metadata["chunk_index"] == 1

    def test_empty_lists(self):
        assert merge_documents([[], []]) == []


# ════════════════════════════════════════════════════
# decompose_question — 复杂问题拆解（LLM mock）
# ════════════════════════════════════════════════════

class TestDecompose:
    def test_decompose_success(self, monkeypatch):
        monkeypatch.setattr(
            agentic, "get_llm",
            lambda **kw: FakeLLM({"sub_questions": ["2024年差旅住宿标准？", "2025年差旅住宿标准？"]}),
        )
        subs = decompose_question("对比2024和2025差旅标准")
        assert subs == ["2024年差旅住宿标准？", "2025年差旅住宿标准？"]

    def test_decompose_insufficient_subs_returns_none(self, monkeypatch):
        """只有 1 个子问题视为拆解失败，回落普通检索"""
        monkeypatch.setattr(
            agentic, "get_llm",
            lambda **kw: FakeLLM({"sub_questions": ["唯一子问题"]}),
        )
        assert decompose_question("问题") is None

    def test_decompose_malformed_returns_none(self, monkeypatch):
        monkeypatch.setattr(agentic, "get_llm", lambda **kw: FakeLLM("随便输出一段文字"))
        assert decompose_question("问题") is None

    def test_decompose_llm_error_returns_none(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("LLM 挂了")
        monkeypatch.setattr(agentic, "get_llm", boom)
        assert decompose_question("问题") is None

    def test_decompose_caps_at_four(self, monkeypatch):
        monkeypatch.setattr(
            agentic, "get_llm",
            lambda **kw: FakeLLM({"sub_questions": ["s1", "s2", "s3", "s4", "s5", "s6"]}),
        )
        subs = decompose_question("问题")
        assert subs is not None and len(subs) == 4


# ════════════════════════════════════════════════════
# extract_calculation — 数值提取 + 程序计算
# ════════════════════════════════════════════════════

class TestExtractCalculation:
    def test_extract_and_compute(self, monkeypatch):
        monkeypatch.setattr(
            agentic, "get_llm",
            lambda **kw: FakeLLM({"expression": "10 - 3", "explanation": "应休年假10天减已休3天"}),
        )
        calc = extract_calculation("我年假还剩几天", "年假10天，已休3天")
        assert calc is not None
        assert calc["result"] == 7
        assert calc["explanation"] == "应休年假10天减已休3天"

    def test_extract_no_numbers_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            agentic, "get_llm",
            lambda **kw: FakeLLM({"expression": None, "explanation": ""}),
        )
        assert extract_calculation("问题", "片段") is None

    def test_extract_injection_expression_returns_none(self, monkeypatch):
        """LLM 被诱导输出注入表达式时，安全求值拒绝并回落 None"""
        monkeypatch.setattr(
            agentic, "get_llm",
            lambda **kw: FakeLLM({"expression": "__import__('os')", "explanation": ""}),
        )
        assert extract_calculation("问题", "片段") is None

    def test_format_note_integer(self):
        note = format_calculation_note({"expression": "10 - 3", "result": 7, "explanation": "应休10天减已休3天"})
        assert "10 - 3 = 7" in note
        assert "程序精确计算" in note

    def test_format_note_float(self):
        note = format_calculation_note({"expression": "2.5 * 4", "result": 10.0, "explanation": ""})
        assert "= 10" in note


# ════════════════════════════════════════════════════
# 验收 A5：无效引用标记清理（"第十四条"误写成"[来源14]"）
# ════════════════════════════════════════════════════

class TestCitationCleanup:
    def test_invalid_marker_removed_valid_kept(self):
        from app.services.chat_service import _remove_invalid_citation_markers

        text = "加班餐费为30元[来源14]。婚假3天[来源1]。"
        cleaned = _remove_invalid_citation_markers(text, {1})
        assert "[来源14]" not in cleaned
        assert "[来源1]" in cleaned

    def test_all_markers_cleaned_when_none_valid(self):
        from app.services.chat_service import _remove_invalid_citation_markers

        assert _remove_invalid_citation_markers("内容[来源3]", set()) == "内容"
