"""角色差异化回答测试 — user / admin 双角色（纯函数直测 + 集成 mock，不调用真实 LLM）"""
import json
import pytest
from unittest.mock import patch
from pydantic import ValidationError
from httpx import AsyncClient
from langchain_core.documents import Document

from app.schemas.chat import ChatRequest
from app.services.chat_service import (
    resolve_effective_role,
    _truncate_answer,
    _trim_dangling_citation,
    _category_path_for,
    _extract_citations,
    _with_history_context,
)
from app.rag.prompts import get_rag_prompt
from app.rag.llm import TokenUsageHandler

# ── 集成测试用的假分类树与假文档 ──

FAKE_CATEGORIES = [
    {
        "id": "cat_phone",
        "name": "手机",
        "description": None,
        "parent_id": None,
        "children": [
            {
                "id": "cat_iphone",
                "name": "iPhone 16",
                "description": None,
                "parent_id": "cat_phone",
                "children": [],
            },
        ],
    },
]

FAKE_DOC = Document(
    page_content="iPhone 16 搭载 A18 仿生芯片，性能提升 30%。",
    metadata={
        "document_id": "doc_1",
        "filename": "手机参数.txt",
        "chunk_index": 0,
        "category_id": "cat_iphone",
        "parent_category_id": "cat_phone",
    },
)


class FakeChain:
    """模拟 RAG 链：按给定分块流式输出"""

    def __init__(self, chunks: list[str]):
        self._chunks = chunks

    async def astream(self, *args, **kwargs):
        for c in self._chunks:
            yield c


def _fake_classify_result(*args, **kwargs):
    return {
        "clarified_question": "iPhone 16 性能怎么样",
        "category_id": "cat_iphone",
        "category_name": "iPhone 16",
    }


def _install_chat_mocks(fake_chunks: list[str]):
    """安装集成测试所需 mock，返回 8 元组
    (classify_patch, retrieve_patch, chain_patch, categories_patch, rerank_patch,
     visible_kb_patch, visible_docs_patch, chunk_count_patch)

    P3-3 注意：rerank 会真实调用 DashScope 重排 API，mock 集成测试必须一并
    patch 为恒等函数，否则测试会走真实网络。
    延迟优化 A+C 注意：count_chunks 固定返回小库（1），保证测试走确定性的
    "小库快路"，不依赖真实测试向量库状态。
    企业知识库阶段1 注意：可见范围（可见库/可见文档）也 mock 为固定值，
    避免集成测试依赖 DB 里的知识库/授权数据。
    """
    classify_patch = patch(
        "app.services.chat_service.classify_question",
        side_effect=_fake_classify_result,
    )
    retrieve_patch = patch(
        "app.services.chat_service.retrieve_only",
        return_value=[FAKE_DOC],
    )
    chain_patch = patch(
        "app.services.chat_service.build_rag_chain",
        return_value=FakeChain(fake_chunks),
    )
    categories_patch = patch(
        "app.services.document_service.get_categories",
        return_value=FAKE_CATEGORIES,
    )
    rerank_patch = patch(
        "app.services.chat_service.rerank_documents",
        side_effect=lambda query, docs, top_n: docs[:top_n],
    )
    visible_kb_patch = patch(
        "app.services.chat_service.kb_service.get_visible_kb_ids",
        return_value=["kb_1"],
    )
    visible_docs_patch = patch(
        "app.services.chat_service.kb_service.get_visible_doc_ids",
        return_value=["doc_1"],
    )
    # 延迟优化 A+C：固定为小库快路（可见分块=1），避免依赖真实测试向量库状态
    chunk_count_patch = patch(
        "app.services.chat_service.count_chunks",
        return_value=1,
    )
    return (
        classify_patch, retrieve_patch, chain_patch, categories_patch,
        rerank_patch, visible_kb_patch, visible_docs_patch, chunk_count_patch,
    )


async def _collect_sse(client: AsyncClient, payload: dict) -> dict[str, list[dict]]:
    """发送流式请求并解析 SSE 事件，返回 {事件名: [data列表]}"""
    events: dict[str, list[dict]] = {}
    async with client.stream("POST", "/api/v1/chat/stream", json=payload) as resp:
        assert resp.status_code == 200
        event_name = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: ") and event_name:
                events.setdefault(event_name, []).append(json.loads(line[len("data: "):]))
    return events


# ════════════════════════════════════════════════════
# 纯函数测试
# ════════════════════════════════════════════════════

class TestChatRequestRole:
    """ChatRequest.role 字段测试"""

    def test_role_defaults_to_user(self):
        """缺省 role 为 user"""
        assert ChatRequest(message="你好").role == "user"

    def test_role_accepts_admin(self):
        """role=admin 合法"""
        assert ChatRequest(message="你好", role="admin").role == "admin"

    def test_role_invalid_value_rejected(self):
        """非法 role 值被 Pydantic 拒绝"""
        with pytest.raises(ValidationError):
            ChatRequest(message="你好", role="bogus")


class TestWithHistoryContext:
    """多轮检索词扩展（把最近一轮对话并入检索词）"""

    def test_no_history_unchanged(self):
        """无历史时原样返回"""
        assert _with_history_context("哪些不辣", None) == "哪些不辣"
        assert _with_history_context("哪些不辣", []) == "哪些不辣"

    def test_history_appended(self):
        """有历史时把上一轮用户/客服的话拼进检索词（指代类追问可命中上文商品）"""
        history = [
            {"role": "user", "content": "有什么零食推荐？"},
            {"role": "assistant", "content": "有鱿鱼仔、锅巴、猪肉脯等"},
        ]
        result = _with_history_context("哪些不辣", history)
        assert "哪些不辣" in result
        assert "有什么零食推荐" in result
        assert "鱿鱼仔" in result

    def test_query_comes_first_then_history(self):
        """检索词结构：本轮问题在前，历史上下文拼接在后（当前问题不会混入历史段）"""
        history = [{"role": "user", "content": "上一轮问题"}]
        result = _with_history_context("本轮问题", history)
        assert result.startswith("本轮问题（上下文：")
        assert "上一轮问题" in result
        # 历史段只出现一次（即拼接的那次），不存在把本轮问题当历史的情况
        assert result.count("本轮问题") == 1

    def test_self_contained_new_question_no_history(self):
        """批次F 验收修复（IT 故障怎么报修）：完整新问题（≥7 字且无指代词）
        不注入上一轮对话——真实验收：先问组织架构再问"IT 故障怎么报修"，
        客服答的组织架构内容把 IT 报障块挤出候选，答案误判"知识库暂无"；
        上一轮答案对新问题纯属噪声"""
        history = [
            {"role": "user", "content": "组织架构是什么样的"},
            {"role": "assistant", "content": "云帆科技实行总经理—总监—经理—员工四级管理架构…"},
        ]
        assert _with_history_context("IT 故障怎么报修", history) == "IT 故障怎么报修"

    def test_referential_question_still_injects_history(self):
        """指代类追问（"那个"）仍注入历史——完整新问题修复不得误伤指代解析"""
        history = [
            {"role": "user", "content": "组织架构是什么样的"},
            {"role": "assistant", "content": "云帆科技实行四级管理架构…"},
        ]
        result = _with_history_context("那个 IT 故障怎么报修呢", history)
        assert "组织架构是什么样的" in result
        assert "IT 故障怎么报修" in result

    def test_bare_pronoun_excluded_in_qita(self):
        """"其他/其它"中的"他/它"不算指代词——"其他部门的考勤怎么算"是自足问题"""
        history = [{"role": "user", "content": "组织架构是什么样的"}]
        assert _with_history_context("其他部门的考勤怎么算", history) == "其他部门的考勤怎么算"

    def test_short_followup_still_injects_history(self):
        """短追问（≤6 字，如"怎么申请"）缺主语，上文是唯一线索——仍注入
        （验收 2.16：先问"年假有几天"再问"怎么申请"要理解成年假申请）"""
        history = [{"role": "user", "content": "年假有几天"}]
        result = _with_history_context("怎么申请", history)
        assert "年假有几天" in result



class TestResolveEffectiveRole:
    """角色校验降级测试"""

    def test_admin_requests_admin_ok(self):
        """admin 用户请求 admin 模式放行"""
        assert resolve_effective_role("admin", "admin") == "admin"

    def test_non_admin_requests_admin_downgraded(self):
        """普通用户请求 admin 模式降级为 user"""
        assert resolve_effective_role("admin", "user") == "user"

    def test_admin_requests_user_stays_user(self):
        """admin 用户请求 user 模式保持 user"""
        assert resolve_effective_role("user", "admin") == "user"


class TestRagPromptByRole:
    """双角色 Prompt 选择测试（企业知识库改造阶段2：员工端同样必须标注引用）"""

    def test_user_prompt_requires_citations(self):
        """员工端 prompt：限长、结论先行、必须标注引用（答案要有出处可查证）"""
        prompt = get_rag_prompt(role="user")
        template = prompt.messages[0].prompt.template
        assert "300字" in template
        assert "必须标注引用" in template
        assert "结论先行" in template
        assert "企业" in template

    def test_user_prompt_has_followup_and_numeric_rules(self):
        """员工端 prompt 含通用追问规则与数值复述纪律"""
        prompt = get_rag_prompt(role="user")
        template = prompt.messages[0].prompt.template
        assert "数值必须逐字复制" in template
        assert "信息不足时可追问" in template

    def test_admin_prompt_full_and_professional(self):
        """admin 角色 prompt 要求完整回答与专业客观"""
        prompt = get_rag_prompt(role="admin")
        template = prompt.messages[0].prompt.template
        assert "完整详尽" in template
        assert "专业客观" in template
        assert "[来源1]" in template

    def test_prompt_with_history_keeps_placeholder(self):
        """带历史时仍保留 MessagesPlaceholder"""
        prompt = get_rag_prompt(with_history=True, role="user")
        assert len(prompt.messages) == 3
        from langchain_core.prompts import MessagesPlaceholder
        assert any(isinstance(m, MessagesPlaceholder) for m in prompt.messages)

    def test_both_roles_context_template_cite_sources(self):
        """员工与管理员上下文模板都要求 [来源N] 引用（阶段2：无差异，出处可查证）"""
        user_template = get_rag_prompt(role="user").messages[-1].prompt.template
        admin_template = get_rag_prompt(role="admin").messages[-1].prompt.template
        assert "[来源N]" in user_template
        assert "[来源N]" in admin_template


class TestTruncateAnswer:
    """句界软截断测试（补句语义：150 字后补完当前句，最多超 20 字）"""

    def test_within_limit_unchanged(self):
        """未超限原文返回"""
        text = "这是一段一百字以内的回答。" * 3
        result, truncated = _truncate_answer(text, 150)
        assert result == text
        assert truncated is False

    def test_truncate_completes_current_sentence(self):
        """句号落在 150~170 窗口内 → 补完当前句（允许小幅超限）"""
        text = ("句" * 159) + "。后续内容不需要的部分也一起放进来"
        assert len(text) > 170
        result, truncated = _truncate_answer(text, 150)
        assert truncated is True
        assert len(result) == 160
        assert result.endswith("。")

    def test_truncate_window_no_boundary_falls_back(self):
        """150~170 窗口内无句界 → 回退到 150 内最后一个有效句界"""
        text = ("句" * 130) + "。" + ("字" * 100)  # 句号在 130，之后无标点
        result, truncated = _truncate_answer(text, 150)
        assert truncated is True
        assert len(result) == 131
        assert result.endswith("。")

    def test_hard_cut_with_ellipsis(self):
        """无句界时硬切并补省略号"""
        text = "字" * 200  # 无任何句末标点
        result, truncated = _truncate_answer(text, 150)
        assert truncated is True
        assert len(result) == 151
        assert result.endswith("…")

    def test_early_boundary_ignored(self):
        """句界太靠前（<50%）视为无效，走硬切"""
        text = "句。" + "字" * 199
        result, truncated = _truncate_answer(text, 150)
        assert truncated is True
        assert result.endswith("…")



class TestTrimDanglingCitation:
    """截断切进 [来源N] 标记内部的清理（企业改造阶段2：员工端保留引用后新增）"""

    def test_dangling_prefix_removed(self):
        """结尾残缺标记（"[来源"、"[来源1"）被剔除"""
        assert _trim_dangling_citation("年假为5天。[来源") == "年假为5天。"
        assert _trim_dangling_citation("年假为5天。[来源1") == "年假为5天。"

    def test_complete_marker_kept(self):
        """完整标记 [来源1] 保留不动"""
        assert _trim_dangling_citation("年假为5天。[来源1]") == "年假为5天。[来源1]"

    def test_no_marker_unchanged(self):
        """无标记文本不变"""
        assert _trim_dangling_citation("年假为5天。") == "年假为5天。"


class TestCategoryPathFor:
    """分类路径解析测试"""

    ID_TO_PATH = {"cat_phone": "手机", "cat_iphone": "手机 > iPhone 16"}

    def test_category_id_hit(self):
        """category_id 命中时返回其路径"""
        meta = {"category_id": "cat_iphone"}
        assert _category_path_for(meta, self.ID_TO_PATH) == "手机 > iPhone 16"

    def test_fallback_to_parent(self):
        """category_id 缺失时回退 parent_category_id 路径"""
        meta = {"parent_category_id": "cat_phone"}
        assert _category_path_for(meta, self.ID_TO_PATH) == "手机"

    def test_unknown_category(self):
        """都缺失时返回未知分类"""
        assert _category_path_for({}, self.ID_TO_PATH) == "未知分类"


class TestExtractCitationsWithPath:
    """引用提取附带分类路径测试"""

    def test_citation_includes_category_path(self):
        """citation dict 包含 category_path"""
        sources = [{
            "index": 1, "doc_id": "doc_1", "title": "手机参数.txt",
            "text": "内容片段", "category_path": "手机 > iPhone 16",
        }]
        citations = _extract_citations("根据[来源1]的内容", sources)
        assert len(citations) == 1
        assert citations[0]["category_path"] == "手机 > iPhone 16"


class TestTokenUsageHandler:
    """Token 用量捕获测试"""

    def test_captures_llm_output(self):
        """从 llm_output 捕获 DashScope 键名用量"""
        handler = TokenUsageHandler()
        fake_response = type("R", (), {
            "llm_output": {"token_usage": {
                "input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
            }},
        })()
        handler.on_llm_end(fake_response)
        assert handler.usage == {
            "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
        }

    def test_no_usage_stays_none(self):
        """无用量信息时保持 None"""
        handler = TokenUsageHandler()
        fake_response = type("R", (), {"llm_output": {}})()
        handler.on_llm_end(fake_response)
        assert handler.usage is None


# ════════════════════════════════════════════════════
# 集成测试（mock LLM 链路）
# ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_chat_stream_user_role_truncates_and_keeps_citations(user_client: AsyncClient):
    """员工端到端（阶段2）：超长回答补句截断（≤320）、保留 [来源N] 引用并落库"""
    # 超长回答，含 [来源1] 标记与句号
    fake_chunks = [
        "根据[来源1]信息：",
        "iPhone 16 采用A18仿生芯片，性能强劲。" * 20,
    ]
    patches = _install_chat_mocks(fake_chunks)
    for p in patches:
        p.start()
    try:
        events = await _collect_sse(user_client, {"message": "iPhone 16 性能怎么样"})

        # done 事件断言
        assert "done" in events
        done = events["done"][-1]
        assert done["role"] == "user"
        assert done["truncated"] is True
        content = done["content"]
        assert len(content) <= 320
        assert content.endswith("。")
        # 阶段2：引用不再被清除（出处可查证）
        assert "[来源1]" in content

        # 员工 sources 事件同样携带原文片段（阶段2 变更）
        sources = events.get("sources", [{}])[-1].get("documents", [])
        assert sources, "应仍有来源事件"
        assert sources[0].get("title") == "手机参数.txt"
        assert "text" in sources[0]

        # 持久化的 assistant 消息：内容与 done 一致，引用已落库
        resp = await user_client.get(f"/api/v1/conversations/{done['conversation_id']}")
        detail = resp.json()
        assistant_msgs = [m for m in detail["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == content
        assert assistant_msgs[0]["citations"] is not None
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_chat_stream_admin_role_full_answer_with_citations(admin_client: AsyncClient):
    """admin 角色端到端：完整回答不截断、引用带分类路径"""
    fake_chunks = ["根据[来源1]，iPhone 16 搭载A18仿生芯片。" * 6]  # 超过 150 字
    patches = _install_chat_mocks(fake_chunks)
    for p in patches:
        p.start()
    try:
        events = await _collect_sse(admin_client, {"message": "iPhone 16 性能怎么样", "role": "admin"})

        done = events["done"][-1]
        assert done["role"] == "admin"
        assert "truncated" not in done

        # 管理员 sources 事件保留原文片段（引用展示用）
        admin_sources = events.get("sources", [{}])[-1].get("documents", [])
        assert admin_sources and "text" in admin_sources[0]

        # 持久化消息：完整文本 + 引用带分类路径
        resp = await admin_client.get(f"/api/v1/conversations/{done['conversation_id']}")
        detail = resp.json()
        assistant_msgs = [m for m in detail["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        saved = assistant_msgs[0]
        assert saved["content"] == "".join(fake_chunks)
        assert saved["citations"] is not None
        assert saved["citations"][0]["category_path"] == "手机 > iPhone 16"
        assert saved["citations"][0]["title"] == "手机参数.txt"
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_chat_stream_non_admin_requesting_admin_downgraded(user_client: AsyncClient):
    """普通用户伪造 role=admin 被降级为 user"""
    fake_chunks = ["iPhone 16 采用A18仿生芯片，性能强劲。"]
    patches = _install_chat_mocks(fake_chunks)
    for p in patches:
        p.start()
    try:
        events = await _collect_sse(user_client, {"message": "iPhone 16 性能怎么样", "role": "admin"})
        done = events["done"][-1]
        assert done["role"] == "user"
    finally:
        for p in patches:
            p.stop()


class FailingChain:
    """模拟 RAG 链：astream 抛异常（用于 LLM 失败路径测试）。

    函数体含 yield（即使不可达），使 astream 返回 async generator，
    与真实 LangChain 链一致；迭代时立即抛异常。
    """

    async def astream(self, *args, **kwargs):
        if False:
            yield  # 使本函数成为 async generator
        raise RuntimeError("dashscope internal error: secret detail /api/v1/foo")


@pytest.mark.asyncio
async def test_chat_stream_llm_error_persists_user_message_generic_error(user_client: AsyncClient):
    """P1-5/P1-6 回归：LLM 中途失败时——
    1) error 事件不回传异常原文（信息泄露修复）；
    2) 用户消息已落库（流式前先 commit 修复）；
    3) message_count 与实际消息数一致（计数一致性修复）。
    """
    classify_patch, retrieve_patch, chain_patch, categories_patch, rerank_patch, \
        visible_kb_patch, visible_docs_patch, chunk_count_patch = _install_chat_mocks([])
    chain_patch = patch(
        "app.services.chat_service.build_rag_chain",
        return_value=FailingChain(),
    )
    patches = [
        classify_patch, retrieve_patch, chain_patch, categories_patch,
        rerank_patch, visible_kb_patch, visible_docs_patch, chunk_count_patch,
    ]
    for p in patches:
        p.start()
    try:
        events = await _collect_sse(user_client, {"message": "iPhone 16 性能怎么样"})

        # error 事件：通用文案，不含异常原文
        assert "error" in events
        error_msg = events["error"][-1]["message"]
        assert "dashscope" not in error_msg
        assert "secret detail" not in error_msg
        assert "出错" in error_msg

        # 会话详情：用户消息已持久化，计数与真实消息数一致（1 条用户消息）
        resp = await user_client.get("/api/v1/conversations")
        conv_id = resp.json()["items"][0]["id"]
        detail = (await user_client.get(f"/api/v1/conversations/{conv_id}")).json()
        assert detail["message_count"] == 1
        assert len(detail["messages"]) == 1
        assert detail["messages"][0]["role"] == "user"
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_chat_stream_message_count_matches_rows(user_client: AsyncClient):
    """P1-5 回归：正常一轮问答后 message_count == 实际消息条数（用户+AI 各 1）"""
    patches = _install_chat_mocks(["iPhone 16 采用A18仿生芯片，性能强劲。"])
    for p in patches:
        p.start()
    try:
        events = await _collect_sse(user_client, {"message": "iPhone 16 性能怎么样"})
        conv_id = events["done"][-1]["conversation_id"]
        detail = (await user_client.get(f"/api/v1/conversations/{conv_id}")).json()
        assert detail["message_count"] == 2
        assert len(detail["messages"]) == 2
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_chat_history_excludes_current_question(client: AsyncClient):
    """P3-2 回归：历史上下文排除当前问题（exclude_message_id），当前问题只传一遍"""
    from datetime import datetime
    from app.infrastructure.database import async_session_factory
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.services.chat_service import _get_chat_history

    async with async_session_factory() as db:
        conv = Conversation(user_id="u1", title="测试会话", status="active")
        db.add(conv)
        await db.flush()
        m1 = Message(conversation_id=conv.id, role="user", content="上一个问题",
                     created_at=datetime(2026, 8, 26, 10, 0, 0))
        m2 = Message(conversation_id=conv.id, role="assistant", content="上一个回答",
                     created_at=datetime(2026, 8, 26, 10, 0, 1))
        m3 = Message(conversation_id=conv.id, role="user", content="当前问题",
                     created_at=datetime(2026, 8, 26, 10, 0, 2))
        db.add_all([m1, m2, m3])
        await db.flush()

        history = await _get_chat_history(db, conv.id, exclude_message_id=m3.id)
        assert [h["content"] for h in history] == ["上一个问题", "上一个回答"]

        # 不传 exclude 时行为不变（当前问题仍在历史里，兼容旧调用方）
        history_all = await _get_chat_history(db, conv.id)
        assert "当前问题" in [h["content"] for h in history_all]


class TestChainReusesRetrievedDocs:
    """P2-3 回归：传入 retrieved_docs 时链内不再二次检索；不传时行为不退化"""

    def test_no_second_retrieval_when_docs_provided(self):
        """传 retrieved_docs → 链内若触发检索则视为失败（patch 直接抛断言）"""
        from langchain_core.runnables import RunnableLambda
        from app.rag.chain import build_rag_chain

        fake_llm = RunnableLambda(lambda _: "测试回答")
        with patch("app.rag.chain.get_llm", return_value=fake_llm), \
             patch("app.rag.chain.get_hybrid_retriever",
                   side_effect=AssertionError("链内不应二次检索")), \
             patch("app.rag.chain.get_retriever",
                   side_effect=AssertionError("链内不应二次检索")):
            chain = build_rag_chain(role="admin", retrieved_docs=[FAKE_DOC])
            output = chain.invoke("iPhone 16 性能怎么样")
        assert output == "测试回答"

    def test_fallback_retrieves_when_docs_omitted(self):
        """不传 retrieved_docs → 仍走混合检索（保证默认行为不退化）"""
        from langchain_core.runnables import RunnableLambda
        from app.rag.chain import build_rag_chain

        fake_llm = RunnableLambda(lambda _: "测试回答")
        fake_retriever = RunnableLambda(lambda q: [FAKE_DOC])
        with patch("app.rag.chain.get_llm", return_value=fake_llm), \
             patch("app.rag.chain.get_hybrid_retriever", return_value=fake_retriever):
            chain = build_rag_chain(role="admin")
            output = chain.invoke("iPhone 16 性能怎么样")
        assert output == "测试回答"
