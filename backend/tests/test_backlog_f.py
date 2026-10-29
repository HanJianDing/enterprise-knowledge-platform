"""批次F 挂清单 #12/#13/#15 攒批修复测试。

- #15 危险操作检测（tools.detect_dangerous_request 纯函数）
- #12 API key 库白名单（admin 创建/列表接口 + OpenAI 门 kb_restrict 透传）
- #13 MCP Server 页面化（admin CRUD + 测试连接错误分支 + 配置缓存刷新）
"""
import pytest

from app.infrastructure.database import async_session_factory


# ── #15 危险操作检测 ──

class TestDangerousRequestDetection:
    """危险操作请求识别（保守白名单式：只匹配明确危险意图，宁漏勿伤）"""

    def _detect(self, q: str) -> bool:
        from app.rag.tools import detect_dangerous_request

        return detect_dangerous_request(q)

    def test_read_local_file_via_python(self):
        """6.9 验收场景：用 python 读本机文件 → 危险"""
        assert self._detect("用 python 读取我电脑里的文件内容") is True

    def test_delete_local_file(self):
        """删除本机文件 → 危险"""
        assert self._detect("帮我删除本地磁盘上的文件") is True

    def test_execute_system_command(self):
        """执行系统命令 → 危险"""
        assert self._detect("执行系统命令帮我清理垃圾") is True

    def test_steal_password(self):
        """窃取密码 → 危险"""
        assert self._detect("窃取管理员的密码") is True

    def test_normal_calculation_not_dangerous(self):
        """纯计算题 → 不危险（不误伤工具路径）"""
        assert self._detect("12345 加 6789 等于多少") is False

    def test_kb_doc_delete_question_not_dangerous(self):
        """管理操作咨询（怎么删除知识库文档）→ 不危险（误伤保护）"""
        assert self._detect("怎么删除知识库里的文档") is False

    def test_normal_qa_not_dangerous(self):
        """普通知识库问答 → 不危险"""
        assert self._detect("年假怎么申请") is False

    def test_empty_question_not_dangerous(self):
        """空问题 → 不危险"""
        assert self._detect("") is False
        assert self._detect("   ") is False

    # ── 质检 P2-1 防误伤回归（三处收紧的对应场景）──

    def test_python_sandbox_calculation_not_dangerous(self):
        """"用 python 执行代码算平均值" → 内置沙箱正面场景，不误拒"""
        assert self._detect("用 python 执行这段代码算平均值") is False

    def test_delete_db_duplicates_question_not_dangerous(self):
        """"怎么删除数据库中的重复记录" → 管理咨询，不误拒"""
        assert self._detect("怎么删除数据库中的重复记录") is False

    def test_get_account_permission_not_dangerous(self):
        """"如何获取账号权限" → 权限咨询，不误拒（获取限他人+密码类）"""
        assert self._detect("如何获取账号权限") is False

    def test_steal_others_password_still_dangerous(self):
        """"获取别人的密码" → 仍危险（收紧后危险意图不失守）"""
        assert self._detect("获取别人的密码") is True

    def test_delete_whole_database_still_dangerous(self):
        """"删除整个数据库" → 仍危险"""
        assert self._detect("删除整个数据库") is True


# ── #12 API key 库白名单 ──

class TestApiKeyKbWhitelist:
    """创建 key 可选库范围；非法库 400；列表回显；门侧透传 kb_restrict"""

    @pytest.mark.asyncio
    async def test_create_with_kb_ids_and_list_echo(
        self, admin_client, default_kb
    ):
        resp = await admin_client.post("/api/v1/admin/api-keys", json={
            "name": "财务库专用 key",
            "kb_ids": [default_kb],
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["kb_ids"] == [default_kb]
        assert body["key"].startswith("sk-rag-")

        lst = await admin_client.get("/api/v1/admin/api-keys")
        assert lst.status_code == 200
        row = next(r for r in lst.json() if r["id"] == body["id"])
        assert row["kb_ids"] == [default_kb]

    @pytest.mark.asyncio
    async def test_create_without_kb_ids_means_passthrough(
        self, admin_client
    ):
        """不传 kb_ids → null（穿透账号权限）"""
        resp = await admin_client.post("/api/v1/admin/api-keys", json={
            "name": "穿透 key",
        })
        assert resp.status_code == 201
        assert resp.json()["kb_ids"] is None

    @pytest.mark.asyncio
    async def test_create_with_unknown_kb_rejected(self, admin_client):
        """库白名单含不存在的库 → 400 可读错误"""
        resp = await admin_client.post("/api/v1/admin/api-keys", json={
            "name": "坏库 key",
            "kb_ids": ["no-such-kb"],
        })
        assert resp.status_code == 400
        assert "知识库不存在" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_with_invalid_kb_ids_shape_rejected(
        self, admin_client
    ):
        """kb_ids 非字符串数组 → 400"""
        resp = await admin_client.post("/api/v1/admin/api-keys", json={
            "name": "坏形状 key",
            "kb_ids": "财务库",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_openai_gate_passes_kb_restrict(
        self, admin_client, default_kb, monkeypatch
    ):
        """OpenAI 门把 key 的 kb_ids 透传为 stream_chat 的 kb_restrict"""
        from app.services import chat_service

        captured: dict = {}

        async def _fake_stream_chat(*args, **kwargs):
            captured["kb_restrict"] = kwargs.get("kb_restrict")
            yield "event: token\ndata: {\"type\": \"text_delta\", \"content\": \"测试回答\"}"
            yield ("event: done\ndata: {\"type\": \"done\", "
                   "\"conversation_id\": \"x\", \"content\": \"测试回答\"}")

        monkeypatch.setattr(chat_service, "stream_chat", _fake_stream_chat)

        resp = await admin_client.post("/api/v1/admin/api-keys", json={
            "name": "门透传测试 key",
            "kb_ids": [default_kb],
        })
        key = resp.json()["key"]

        plain_client = admin_client
        # 换 Authorization 为 API key（admin_client 带 JWT）
        old_auth = plain_client.headers.pop("Authorization", None)
        try:
            plain_client.headers["Authorization"] = f"Bearer {key}"
            r = await plain_client.post("/v1/chat/completions", json={
                "model": "rag-knowledge-base",
                "messages": [{"role": "user", "content": "年假怎么申请"}],
                "stream": False,
            })
            assert r.status_code == 200
            assert captured.get("kb_restrict") == [default_kb]
        finally:
            plain_client.headers.pop("Authorization", None)
            if old_auth:
                plain_client.headers["Authorization"] = old_auth

    @pytest.mark.asyncio
    async def test_openai_gate_stream_branch_passes_kb_restrict(
        self, admin_client, default_kb, monkeypatch
    ):
        """质检 P1-1 补测：流式分支（stream=True）同样透传 kb_restrict——
        此前漏传导致白名单在流式路径旁路（fail-open）"""
        from app.services import chat_service

        captured: dict = {}

        async def _fake_stream_chat(*args, **kwargs):
            captured["kb_restrict"] = kwargs.get("kb_restrict")
            yield ("event: token\ndata: {\"type\": \"text_delta\", "
                   "\"content\": \"测试\"}")

        monkeypatch.setattr(chat_service, "stream_chat", _fake_stream_chat)

        resp = await admin_client.post("/api/v1/admin/api-keys", json={
            "name": "流式透传测试 key",
            "kb_ids": [default_kb],
        })
        key = resp.json()["key"]

        old_auth = admin_client.headers.pop("Authorization", None)
        try:
            admin_client.headers["Authorization"] = f"Bearer {key}"
            r = await admin_client.post("/v1/chat/completions", json={
                "model": "rag-knowledge-base",
                "messages": [{"role": "user", "content": "年假怎么申请"}],
                "stream": True,
            })
            assert r.status_code == 200
            assert captured.get("kb_restrict") == [default_kb]
        finally:
            admin_client.headers.pop("Authorization", None)
            if old_auth:
                admin_client.headers["Authorization"] = old_auth

    @pytest.mark.asyncio
    async def test_stream_chat_kb_restrict_converges_doc_scope(
        self, admin_client, default_kb, private_kb, monkeypatch
    ):
        """kb_restrict 与账号可见范围取交集：白名单外库被收敛。

        上传 default_kb 文档后直调 stream_chat（kb_restrict=[private_kb]），
        monkeypatch 检索捕获 doc_ids 验证收敛、web 兜底返回空（不跑 LLM）——
        白名单只含私有库，default_kb 文档应被滤掉 → doc_ids 为空列表。"""
        from sqlalchemy import select as sa_select

        from app.models.conversation import Conversation
        from app.models.user import User
        from app.rag import web_search as ws_mod
        from app.services import chat_service

        # 造 default_kb 文档（fake_document_processing：Document 行真实入表）
        files = {"file": ("kb_scope_test.txt", "范围收敛测试内容".encode(), "text/plain")}
        up = await admin_client.post(
            "/api/v1/knowledge/documents/upload",
            files=files, data={"kb_id": default_kb},
        )
        assert up.status_code in (200, 201)

        captured: dict = {}
        events: list[str] = []

        def _fake_retrieve_only(*args, **kwargs):
            captured["doc_ids"] = list(kwargs.get("doc_ids") or [])
            return []

        async def _fake_search_web(*args, **kwargs):
            return []

        monkeypatch.setattr(chat_service, "retrieve_only", _fake_retrieve_only)
        monkeypatch.setattr(ws_mod, "search_web", _fake_search_web)

        async with async_session_factory() as db:
            admin = (await db.execute(
                sa_select(User).where(User.role == "admin").limit(1)
            )).scalar_one()
            conv = Conversation(user_id=admin.id, title="t")
            db.add(conv)
            await db.commit()
            await db.refresh(conv)
            gen = chat_service.stream_chat(
                db, admin, conv.id, "测试问题", "admin",
                kb_restrict=[private_kb],
            )
            async for event in gen:
                events.append(event)
        # 收敛验证：白名单外库（default_kb）文档被滤掉 → doc_ids 空 →
        # 走"没有可访问知识库内容"提示分支（检索/LLM 均未发生）
        assert captured.get("doc_ids") is None  # retrieve_only 从未被调用
        assert any("没有可访问" in e for e in events)


# ── tool_mode 空文档提示（#13 端到端验证：提示与工具结果脱钩）──

class TestToolModeDocFormat:
    """质检 P3 补测：_format_docs 工具模式空文档提示"""

    def test_tool_mode_empty_docs_hint(self):
        from app.rag.chain import _format_docs

        out = _format_docs([], tool_mode=True)
        assert "直接基于上文工具查询结果" in out
        assert "告知用户暂无相关信息" not in out

    def test_default_empty_docs_hint(self):
        from app.rag.chain import _format_docs

        out = _format_docs([])
        assert "告知用户暂无相关信息" in out


# ── 工具闸门中文数字扩展（#13 加测发现）──

class TestShouldTryToolsChinese:
    """启发式闸门覆盖中文数字与计算意图词（沙箱随机抽样场景漏检）"""

    def _should(self, q: str) -> bool:
        from app.rag.tools import should_try_tools

        return should_try_tools(q)

    def test_chinese_number_triggers(self):
        """中文数字问题进工具检测（"帮我抽三个随机数"此前被漏掉）"""
        assert self._should("帮我抽三个随机数") is True

    def test_random_keyword_triggers(self):
        assert self._should("帮我随机选一个") is True

    def test_average_keyword_triggers(self):
        assert self._should("这几笔报销求个平均") is True

    def test_plain_qa_does_not_trigger(self):
        """普通知识库问答不进工具检测（闸门不放宽到日常问题）"""
        assert self._should("年假怎么申请") is False
        assert self._should("会议室怎么预订") is False

    def test_daily_phrases_do_not_trigger(self):
        """质检 P2-4 防误伤：日常高频词（一起/一般/十分/五险一金）不触发"""
        assert self._should("五险一金怎么缴纳") is False
        assert self._should("一般多久能收到货") is False
        assert self._should("我们一起商量一下排班") is False

    def test_quantifier_chinese_number_triggers(self):
        """量词语境中文数字触发（三个人/五天）"""
        assert self._should("三个人出差住几间房") is True

    def test_arabic_number_still_triggers(self):
        assert self._should("算一下 15*8+12") is True


# ── #13 工具参数错误重试（端到端验证发现）──

class TestToolParamErrorRetry:
    """参数类错误判定 + 带反馈重试决策（小模型参数遵从不稳定）"""

    def test_param_error_detection(self):
        from app.services.chat_service import _tool_result_param_error

        assert _tool_result_param_error(
            "MCP error -32602: Input validation error: Invalid arguments for tool echo: "
            "Invalid input: expected string, received undefined at message"
        )
        assert _tool_result_param_error("计算失败：表达式不合法（1+）")
        assert _tool_result_param_error("（代码执行完成，无输出）")  # 沙箱无输出重试
        assert not _tool_result_param_error("Echo: 外部工具测试成功")
        assert not _tool_result_param_error("12345 加 6789 = 19134")

    @pytest.mark.asyncio
    async def test_detect_with_feedback_injects_error_text(self, monkeypatch):
        """feedback 传入后决策提示词带错误反馈（重试决策的 prompt 构造）"""
        from app.rag import tools as tools_mod

        captured: dict = {}

        class FakeLLM:
            def invoke(self, prompt):
                captured["prompt"] = prompt
                class FakeResp:
                    content = ('{"needs_tool": true, "tool": "test-tools.echo", '
                               '"args": {"message": "hello"}}')
                return FakeResp()

        async def _fake_run_in_executor(fn, *args):
            return fn(*args)

        import app.rag.llm as llm_mod

        monkeypatch.setattr(llm_mod, "run_in_llm_executor", _fake_run_in_executor)
        monkeypatch.setattr(tools_mod, "get_llm", lambda **k: FakeLLM())

        decision = await tools_mod.detect_tool_call(
            "用 echo 工具说：你好",
            [{"name": "test-tools.echo", "description": "回显",
              "args_schema": '{"message": {"type": "string"}}'}],
            feedback="test-tools.echo 返回：MCP error -32602: Input validation error",
        )
        assert decision is not None
        assert decision["args"] == {"message": "hello"}
        assert "上次调用失败" in captured["prompt"]
        assert "Input validation error" in captured["prompt"]
        assert "message" in captured["prompt"]  # 参数声明在提示词里


# ── #13 MCP Server 页面化 ──

class TestMcpServerAdmin:
    """管理接口 CRUD + 测试连接错误分支 + 配置缓存刷新"""

    @pytest.mark.asyncio
    async def test_create_stdio_and_list(self, admin_client):
        resp = await admin_client.post("/api/v1/admin/mcp-servers", json={
            "name": "oa-tools", "transport": "stdio",
            "command": "npx", "args": ["-y", "some-pkg"],
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "oa-tools"
        assert body["transport"] == "stdio"
        assert body["args"] == ["-y", "some-pkg"]
        assert body["enabled"] is True

        lst = await admin_client.get("/api/v1/admin/mcp-servers")
        assert lst.status_code == 200
        assert any(s["name"] == "oa-tools" for s in lst.json())

    @pytest.mark.asyncio
    async def test_create_http(self, admin_client):
        resp = await admin_client.post("/api/v1/admin/mcp-servers", json={
            "name": "oa-http", "transport": "http",
            "url": "http://oa.internal/mcp/",
        })
        assert resp.status_code == 201
        assert resp.json()["url"] == "http://oa.internal/mcp/"

    @pytest.mark.asyncio
    async def test_create_duplicate_name_rejected(self, admin_client):
        await admin_client.post("/api/v1/admin/mcp-servers", json={
            "name": "dup-server", "transport": "http",
            "url": "http://x/mcp/",
        })
        resp = await admin_client.post("/api/v1/admin/mcp-servers", json={
            "name": "dup-server", "transport": "http",
            "url": "http://y/mcp/",
        })
        assert resp.status_code == 400
        assert "已存在" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_invalid_transport_rejected(self, admin_client):
        resp = await admin_client.post("/api/v1/admin/mcp-servers", json={
            "name": "bad-t", "transport": "sse",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_stdio_missing_command_rejected(self, admin_client):
        resp = await admin_client.post("/api/v1/admin/mcp-servers", json={
            "name": "no-cmd", "transport": "stdio",
        })
        assert resp.status_code == 400
        assert "command" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_http_missing_url_rejected(self, admin_client):
        resp = await admin_client.post("/api/v1/admin/mcp-servers", json={
            "name": "no-url", "transport": "http",
        })
        assert resp.status_code == 400
        assert "url" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_toggle_disable_and_delete(self, admin_client):
        resp = await admin_client.post("/api/v1/admin/mcp-servers", json={
            "name": "toggle-me", "transport": "http",
            "url": "http://x/mcp/",
        })
        sid = resp.json()["id"]

        off = await admin_client.patch(
            f"/api/v1/admin/mcp-servers/{sid}/toggle", json={"enabled": False}
        )
        assert off.status_code == 200
        assert off.json()["enabled"] is False

        on = await admin_client.patch(
            f"/api/v1/admin/mcp-servers/{sid}/toggle", json={"enabled": True}
        )
        assert on.json()["enabled"] is True

        deleted = await admin_client.delete(f"/api/v1/admin/mcp-servers/{sid}")
        assert deleted.status_code == 200
        lst = await admin_client.get("/api/v1/admin/mcp-servers")
        assert not any(s["id"] == sid for s in lst.json())

    @pytest.mark.asyncio
    async def test_delete_unknown_404(self, admin_client):
        resp = await admin_client.delete("/api/v1/admin/mcp-servers/no-such")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_test_endpoint_bad_transport(self, admin_client):
        """测试连接：非法 transport → ok:false 可读错误（不抛 500）"""
        resp = await admin_client.post("/api/v1/admin/mcp-servers/test", json={
            "transport": "sse",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "transport" in body["error"]

    @pytest.mark.asyncio
    async def test_test_endpoint_stdio_missing_command(self, admin_client):
        resp = await admin_client.post("/api/v1/admin/mcp-servers/test", json={
            "transport": "stdio",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    @pytest.mark.asyncio
    async def test_refresh_mcp_config_loads_db_rows(self, admin_client):
        """创建后 refresh_mcp_config → 缓存含 DB 配置（问答链路同款刷新）"""
        from app.rag import tools as tools_mod

        await admin_client.post("/api/v1/admin/mcp-servers", json={
            "name": "refresh-me", "transport": "http",
            "url": "http://x/mcp/",
        })
        tools_mod.invalidate_mcp_config()
        await tools_mod.refresh_mcp_config()
        parsed = tools_mod._parse_mcp_servers()
        assert any(s["name"] == "refresh-me" and s["url"] == "http://x/mcp/"
                   for s in parsed)
        # 清理缓存，避免影响其他用例
        tools_mod.invalidate_mcp_config()
