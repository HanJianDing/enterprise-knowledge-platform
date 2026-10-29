"""批次C·11c MCP client 增强测试：双 transport 配置解析 / stdio 会话缓存"""
import pytest

from app.rag import tools


class TestParseMcpServers:
    def test_http_config(self, monkeypatch):
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "mcp_servers",
                            '[{"name": "oa", "url": "http://oa.internal/mcp/"}]')
        servers = tools._parse_mcp_servers()
        assert servers == [{"name": "oa", "transport": "http",
                            "url": "http://oa.internal/mcp/"}]

    def test_stdio_config(self, monkeypatch):
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "mcp_servers",
                            '[{"name": "files", "command": "npx",'
                            '  "args": ["-y", "some-server", "/data"]}]')
        servers = tools._parse_mcp_servers()
        assert servers == [{"name": "files", "transport": "stdio",
                            "command": "npx",
                            "args": ["-y", "some-server", "/data"]}]

    def test_mixed_and_invalid(self, monkeypatch):
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "mcp_servers",
                            '[{"name": "a", "url": "http://x"},'
                            ' {"name": "b", "command": "python", "args": ["s.py"]},'
                            ' {"name": "c"}, {"bad": 1}, "not-dict"]')
        servers = tools._parse_mcp_servers()
        assert [s["name"] for s in servers] == ["a", "b"]

    def test_invalid_json_ignored(self, monkeypatch):
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "mcp_servers", "不是JSON")
        assert tools._parse_mcp_servers() == []

    def test_args_string_not_split_by_char(self, monkeypatch):
        """批次C 质检 P3 回归钉住：args 误配成字符串时按单参数处理，
        不静默按字符拆碎（"abc"→["a","b","c"] 是静默错误）"""
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "mcp_servers",
                            '[{"name": "s", "command": "python", "args": "server.py"}]')
        servers = tools._parse_mcp_servers()
        assert servers[0]["args"] == ["server.py"]


class TestFormatToolsText:
    """批次F 验收修复：工具候选转决策提示词（参数声明进 prompt）"""

    def test_with_schema_includes_params(self):
        text = tools.format_tools_text([{
            "name": "echo", "description": "回显输入",
            "args_schema": '{"message": {"type": "string"}}',
        }])
        assert "echo" in text
        assert "回显输入" in text
        assert '"message"' in text  # 参数声明进了提示词

    def test_without_schema_no_params_line(self):
        text = tools.format_tools_text([{
            "name": "datetime", "description": "查询当前时间",
        }])
        assert "datetime" in text
        assert "参数" not in text  # 无参数工具不渲染参数行

    def test_empty_schema_skipped(self):
        text = tools.format_tools_text([{
            "name": "t", "description": "d", "args_schema": "",
        }])
        assert "参数" not in text


class TestStdioSession:
    """批次F 验收修复：会话用完即弃——跨请求缓存会触发 mcp SDK 的
    cancel scope 跨 task 异常（真实验收 SSE 500），每次调用独立起进程"""

    @pytest.mark.asyncio
    async def test_get_returns_streams_and_session(self, monkeypatch):
        startup_count = {"n": 0}

        class FakeSession:
            def __init__(self, *a, **k):
                pass

            # session 必须先 __aenter__（启动接收循环）再 initialize
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def initialize(self):
                pass

            async def list_tools(self):
                class T:
                    name = "t1"
                    description = "d1"
                    # 外部工具带参数声明（决策提示词用）
                    inputSchema = {"message": {"type": "string"}}
                class R:
                    tools = [T()]
                return R()

        class FakeStreams:
            async def __aenter__(self):
                return object(), object()

            async def __aexit__(self, *a):
                pass

        def fake_stdio_client(params):
            startup_count["n"] += 1
            return FakeStreams()

        monkeypatch.setattr("mcp.client.stdio.stdio_client", fake_stdio_client)
        monkeypatch.setattr("mcp.client.session.ClientSession", FakeSession)

        cfg = {"name": "s1", "transport": "stdio",
               "command": "python", "args": ["x.py"]}
        got = await tools._get_stdio_session("s1", cfg)
        assert got is not None
        streams, session = got
        assert isinstance(session, FakeSession)
        # 用完即弃：每次调用起新进程（不再缓存复用）
        got2 = await tools._get_stdio_session("s1", cfg)
        assert got2 is not None
        assert got2[1] is not session
        assert startup_count["n"] == 2

        # 清理：对称退出两个会话
        await tools._close_stdio_session(streams, session, "s1")
        await tools._close_stdio_session(got2[0], got2[1], "s1")

    @pytest.mark.asyncio
    async def test_startup_failure_returns_none(self, monkeypatch):
        def fake_stdio_client(params):
            raise RuntimeError("启动失败")

        monkeypatch.setattr("mcp.client.stdio.stdio_client", fake_stdio_client)
        cfg = {"name": "s2", "transport": "stdio",
               "command": "python", "args": ["x.py"]}
        assert await tools._get_stdio_session("s2", cfg) is None

    @pytest.mark.asyncio
    async def test_close_exits_streams_and_session(self, monkeypatch):
        class FakeStreams:
            exited = False

            async def __aenter__(self):
                return object(), object()

            async def __aexit__(self, *a):
                FakeStreams.exited = True

        class FakeSession:
            exited = False

            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                FakeSession.exited = True

            async def initialize(self):
                pass

        monkeypatch.setattr("mcp.client.stdio.stdio_client",
                            lambda params: FakeStreams())
        monkeypatch.setattr("mcp.client.session.ClientSession", FakeSession)
        cfg = {"name": "s3", "transport": "stdio",
               "command": "python", "args": ["x.py"]}
        got = await tools._get_stdio_session("s3", cfg)
        assert got is not None
        await tools._close_stdio_session(got[0], got[1], "s3")
        assert FakeStreams.exited  # 进程上下文已退出
        assert FakeSession.exited  # session task group 已退出
