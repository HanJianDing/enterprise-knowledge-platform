"""批次E·6c Python SDK 测试：chat/retrieve 薄封装与参数透传"""
import sys
from pathlib import Path

import pytest

SDK_DIR = Path(__file__).resolve().parents[1] / "sdk"
if str(SDK_DIR) not in sys.path:
    sys.path.insert(0, str(SDK_DIR))

from rag_kb_client import RagKbClient  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestRagKbClient:
    def test_chat_returns_answer_content(self, monkeypatch):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update({"url": url, "json": json, "headers": headers})
            return _FakeResp({"choices": [{"message": {
                "role": "assistant", "content": "年假 5 天"}}]})

        import httpx

        monkeypatch.setattr(httpx, "post", fake_post)
        client = RagKbClient("http://localhost:8000", "sk-test")
        assert client.chat("年假怎么申请？") == "年假 5 天"
        assert captured["url"] == "http://localhost:8000/v1/chat/completions"
        assert captured["json"]["messages"] == [
            {"role": "user", "content": "年假怎么申请？"}]
        assert captured["json"]["stream"] is False
        assert captured["headers"]["Authorization"] == "Bearer sk-test"

    def test_chat_with_history_and_stream(self, monkeypatch):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(json)
            return _FakeResp({"choices": []})

        import httpx

        monkeypatch.setattr(httpx, "post", fake_post)
        client = RagKbClient("http://x", "sk")
        client.chat("追问", history=[{"role": "user", "content": "前问"}],
                    stream=True)
        assert captured["messages"][-1] == {"role": "user", "content": "追问"}
        assert captured["stream"] is True

    def test_retrieve_returns_records_and_passes_settings(self, monkeypatch):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update({"url": url, "json": json})
            return _FakeResp({"records": [{"content": "内容", "score": 0.9,
                                           "title": "文档", "metadata": {}}]})

        import httpx

        monkeypatch.setattr(httpx, "post", fake_post)
        client = RagKbClient("http://x/", "sk")
        records = client.retrieve("问题", top_k=5, score_threshold=0.4,
                                  knowledge_id="kb:abc")
        assert records[0]["title"] == "文档"
        assert captured["url"] == "http://x/v1/dify/retrieval"
        assert captured["json"]["knowledge_id"] == "kb:abc"
        assert captured["json"]["retrieval_setting"] == {
            "top_k": 5, "score_threshold": 0.4}

    def test_http_error_raises(self, monkeypatch):
        class FakeErrResp:
            status_code = 500

            def raise_for_status(self):
                raise RuntimeError("HTTP 500")

        def fake_post(url, **k):
            return FakeErrResp()

        import httpx

        monkeypatch.setattr(httpx, "post", fake_post)
        client = RagKbClient("http://x", "sk")
        with pytest.raises(RuntimeError):
            client.chat("问题")
