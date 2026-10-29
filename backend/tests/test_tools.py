"""产品化 B3 测试：工具调用循环（内置工具 / 决策解析 / 注入格式）"""
import json

import pytest

from app.rag import tools
import app.rag.tools as tools_mod
from app.rag.tools import (
    execute_builtin_tool,
    should_try_tools,
    detect_tool_call,
    execute_tool_call,
    format_tool_note,
    list_external_tools,
)


class FakeLLM:
    def __init__(self, payload: dict | str):
        self.payload = payload

    def invoke(self, prompt: str):
        class Resp:
            content = json.dumps(self.payload) if isinstance(self.payload, dict) else self.payload
        return Resp()


# ════════════════════════════════════════════════════
# 内置工具
# ════════════════════════════════════════════════════

class TestBuiltinTools:
    def test_calculator_ok(self):
        result = execute_builtin_tool("calculator", {"expression": "10 - 3"})
        assert "10 - 3 = 7" in result

    def test_calculator_illegal_expression(self):
        result = execute_builtin_tool("calculator", {"expression": "__import__('os')"})
        assert "计算失败" in result

    def test_datetime_contains_weekday(self):
        result = execute_builtin_tool("datetime", {})
        assert "星期" in result

    def test_unknown_builtin_returns_none(self):
        assert execute_builtin_tool("not-exist", {}) is None


# ════════════════════════════════════════════════════
# 决策与执行
# ════════════════════════════════════════════════════

CANDIDATES = [
    {"name": "calculator", "description": "计算算术表达式"},
    {"name": "datetime", "description": "查询当前日期时间"},
]


class TestDetectToolCall:
    @pytest.mark.asyncio
    async def test_detect_calculator(self, monkeypatch):
        monkeypatch.setattr(
            tools, "get_llm",
            lambda **kw: FakeLLM({"needs_tool": True, "tool": "calculator",
                                  "args": {"expression": "10-3"}}),
        )
        result = await detect_tool_call("我年假还剩几天", CANDIDATES)
        assert result == {"tool": "calculator", "args": {"expression": "10-3"}}

    @pytest.mark.asyncio
    async def test_detect_no_tool_needed(self, monkeypatch):
        monkeypatch.setattr(
            tools, "get_llm",
            lambda **kw: FakeLLM({"needs_tool": False, "tool": None, "args": {}}),
        )
        assert await detect_tool_call("报销流程是什么", CANDIDATES) is None

    @pytest.mark.asyncio
    async def test_detect_hallucinated_tool_rejected(self, monkeypatch):
        """LLM 幻觉出候选外的工具名 → 忽略（防任意工具调用）"""
        monkeypatch.setattr(
            tools, "get_llm",
            lambda **kw: FakeLLM({"needs_tool": True, "tool": "delete_all",
                                  "args": {}}),
        )
        assert await detect_tool_call("帮我删库", CANDIDATES) is None

    @pytest.mark.asyncio
    async def test_detect_malformed_output(self, monkeypatch):
        monkeypatch.setattr(tools, "get_llm", lambda **kw: FakeLLM("随便说点什么"))
        assert await detect_tool_call("问题", CANDIDATES) is None

    @pytest.mark.asyncio
    async def test_execute_tool_call_builtin(self):
        result = await execute_tool_call(
            {"tool": "calculator", "args": {"expression": "2*3"}}
        )
        assert "= 6" in result

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_returns_none(self):
        assert await execute_tool_call({"tool": "ghost", "args": {}}) is None


class TestHeuristics:
    def test_numbers_trigger_tools(self, monkeypatch):
        # 隔离 .env 的 MCP_SERVERS（配置了 MCP 时闸门恒 True，
        # 本类测的是"无 MCP 配置"的启发式行为）
        monkeypatch.setattr(tools_mod, "_parse_mcp_servers", lambda: [])
        assert should_try_tools("报销标准 100 元怎么算") is True

    def test_datetime_words_trigger_tools(self, monkeypatch):
        monkeypatch.setattr(tools_mod, "_parse_mcp_servers", lambda: [])
        assert should_try_tools("今天星期几") is True

    def test_plain_question_skips_tools(self, monkeypatch):
        monkeypatch.setattr(tools_mod, "_parse_mcp_servers", lambda: [])
        assert should_try_tools("年假怎么申请") is False


class TestExternal:
    @pytest.mark.asyncio
    async def test_no_configured_servers_returns_empty(self):
        assert await list_external_tools() == []


class TestFormatNote:
    def test_note_marks_program_result(self):
        note = format_tool_note("calculator", "10 - 3 = 7")
        assert "【工具查询结果】calculator" in note
        assert "程序执行" in note
