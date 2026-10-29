"""产品化 B1 测试：分类器复杂度字段解析（LLM mock）"""
import json

import pytest

from app.rag import classifier as classifier_module
from app.rag.classifier import classify_question, _parse_complexity

CATS = [{"id": "cat1", "name": "休假制度", "path": "休假制度"}]


class FakeLLM:
    def __init__(self, payload: dict):
        self.payload = payload

    def invoke(self, prompt: str):
        class Resp:
            content = json.dumps(self.payload)
        return Resp()


class TestParseComplexity:
    def test_valid_values(self):
        assert _parse_complexity({"complexity": "compare"}) == "compare"
        assert _parse_complexity({"complexity": "CALCULATE"}) == "calculate"

    def test_missing_defaults_simple(self):
        assert _parse_complexity({}) == "simple"

    def test_invalid_defaults_simple(self):
        assert _parse_complexity({"complexity": "hack"}) == "simple"


class TestClassifyComplexity:
    def test_compare_complexity_returned(self, monkeypatch):
        monkeypatch.setattr(
            classifier_module, "get_llm",
            lambda **kw: FakeLLM({
                "clarified_question": "年假和病假对比", "category_id": None,
                "category_name": "", "complexity": "compare",
            }),
        )
        result = classify_question("年假和病假有什么区别", CATS)
        assert result is not None
        assert result["complexity"] == "compare"

    def test_calculate_complexity_with_category(self, monkeypatch):
        monkeypatch.setattr(
            classifier_module, "get_llm",
            lambda **kw: FakeLLM({
                "clarified_question": "年假剩余天数", "category_id": "cat1",
                "category_name": "休假制度", "complexity": "calculate",
            }),
        )
        result = classify_question("我年假还剩几天", CATS)
        assert result is not None
        assert result["category_id"] == "cat1"
        assert result["complexity"] == "calculate"

    def test_missing_complexity_field_still_works(self, monkeypatch):
        """旧版 LLM 输出（无 complexity 字段）不回退失败，默认为 simple"""
        monkeypatch.setattr(
            classifier_module, "get_llm",
            lambda **kw: FakeLLM({
                "clarified_question": "年假政策", "category_id": None,
                "category_name": "",
            }),
        )
        result = classify_question("年假怎么请", CATS)
        assert result is not None
        assert result["complexity"] == "simple"
