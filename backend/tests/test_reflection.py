"""批次C·11b 反思循环测试：自检判定 / 修正反馈 / 异常静默降级"""
import pytest

from app.rag.agentic import reflect_answer


class _FakeResp:
    def __init__(self, text: str):
        self.content = text


class TestReflectAnswer:
    @pytest.mark.asyncio
    async def test_pass_verdict(self, monkeypatch):
        async def fake_invoke(llm, prompt, callbacks=None):
            return _FakeResp('{"pass": true, "feedback": ""}')

        monkeypatch.setattr("app.rag.llm.invoke_with_timeout", fake_invoke)
        result = await reflect_answer("问题", "回答", ["子问题1"])
        assert result is not None
        assert result["pass"] is True
        assert result["feedback"] == ""

    @pytest.mark.asyncio
    async def test_fail_verdict_with_feedback(self, monkeypatch):
        async def fake_invoke(llm, prompt, callbacks=None):
            return _FakeResp(
                '{"pass": false, "feedback": "未覆盖子问题2，请补充"}'
            )

        monkeypatch.setattr("app.rag.llm.invoke_with_timeout", fake_invoke)
        result = await reflect_answer("问题", "回答", ["子问题1", "子问题2"])
        assert result is not None
        assert result["pass"] is False
        assert "子问题2" in result["feedback"]

    @pytest.mark.asyncio
    async def test_llm_timeout_returns_none(self, monkeypatch):
        """超时（invoke_with_timeout 返回 None）→ 静默跳过（None）"""
        async def fake_invoke(llm, prompt, callbacks=None):
            return None

        monkeypatch.setattr("app.rag.llm.invoke_with_timeout", fake_invoke)
        assert await reflect_answer("问题", "回答", []) is None

    @pytest.mark.asyncio
    async def test_malformed_json_returns_none(self, monkeypatch):
        async def fake_invoke(llm, prompt, callbacks=None):
            return _FakeResp("这不是 JSON，随便说点什么")

        monkeypatch.setattr("app.rag.llm.invoke_with_timeout", fake_invoke)
        assert await reflect_answer("问题", "回答", []) is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, monkeypatch):
        async def fake_invoke(llm, prompt, callbacks=None):
            raise RuntimeError("上游爆炸")

        monkeypatch.setattr("app.rag.llm.invoke_with_timeout", fake_invoke)
        assert await reflect_answer("问题", "回答", []) is None

    @pytest.mark.asyncio
    async def test_string_false_not_treated_as_pass(self, monkeypatch):
        """批次C 质检 P3 回归钉住：LLM 输出字符串 "false" 时不得被判为
        通过（bool("false") 恒 True 的经典陷阱）"""
        async def fake_invoke(llm, prompt, callbacks=None):
            return _FakeResp('{"pass": "false", "feedback": "漏答子问题"}')

        monkeypatch.setattr("app.rag.llm.invoke_with_timeout", fake_invoke)
        result = await reflect_answer("问题", "回答", ["子问题"])
        assert result is not None
        assert result["pass"] is False
        assert "漏答子问题" in result["feedback"]

    @pytest.mark.asyncio
    async def test_string_true_parsed_as_pass(self, monkeypatch):
        async def fake_invoke(llm, prompt, callbacks=None):
            return _FakeResp('{"pass": "true", "feedback": ""}')

        monkeypatch.setattr("app.rag.llm.invoke_with_timeout", fake_invoke)
        result = await reflect_answer("问题", "回答", [])
        assert result is not None
        assert result["pass"] is True
        assert result["feedback"] == ""

    @pytest.mark.asyncio
    async def test_unparsable_pass_returns_none(self, monkeypatch):
        async def fake_invoke(llm, prompt, callbacks=None):
            return _FakeResp('{"pass": "maybe", "feedback": ""}')

        monkeypatch.setattr("app.rag.llm.invoke_with_timeout", fake_invoke)
        assert await reflect_answer("问题", "回答", []) is None
