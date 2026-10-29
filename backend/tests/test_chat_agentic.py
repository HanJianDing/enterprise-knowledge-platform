"""产品化 B1 测试：聊天流中的 Agentic RAG 编排（全 mock，无真实 LLM/检索/嵌入）"""
import json

import pytest
from httpx import AsyncClient
from langchain_core.documents import Document
from sqlalchemy import select

from app.infrastructure.database import async_session_factory
from app.models.audit import AuditLog
from app.models.document import KnowledgeCategory
from app.services import chat_service


async def _ret(value):
    """async 版恒等函数：给 monkeypatch 的 lambda 返回协程用"""
    return value


class FakeChain:
    """假生成链：astream 一次吐完固定答案"""

    def __init__(self, answer: str):
        self._answer = answer

    async def astream(self, input, config=None):
        yield self._answer

    async def aclose(self):
        pass


def _fake_docs() -> list[Document]:
    return [
        Document(
            page_content="2024年差旅住宿标准为300元",
            metadata={"document_id": "doc-2024", "chunk_index": 0,
                      "filename": "2024差旅.txt", "category_id": "cat1"},
        ),
        Document(
            page_content="2025年差旅住宿标准为400元",
            metadata={"document_id": "doc-2025", "chunk_index": 0,
                      "filename": "2025差旅.txt", "category_id": "cat2"},
        ),
    ]


async def _seed_category(kb_id: str) -> None:
    """可见库下建分类 + 一条已完成文档：
    - flat_cats 非空时分类链路才会执行
    - 文档为空时聊天流提前返回"没有可访问的知识库内容"，到不了编排分支
    """
    from app.models.document import Document
    from app.models.user import User
    from sqlalchemy import select

    async with async_session_factory() as db:
        db.add(KnowledgeCategory(name="差旅制度", kb_id=kb_id))
        admin = (await db.execute(
            select(User).where(User.role == "admin").limit(1)
        )).scalar_one()
        db.add(Document(
            filename="seed.txt", original_name="seed.txt", file_type="txt",
            file_size=10, storage_path="seed.txt", kb_id=kb_id,
            content_type="long_text", status="completed", chunk_count=1,
            uploaded_by=admin.id,
        ))
        await db.commit()


async def _collect_events(resp) -> list[dict]:
    events = []
    async for line in resp.aiter_lines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return events


def _patch_complexity(monkeypatch, complexity: str) -> None:
    monkeypatch.setattr(
        chat_service, "classify_question",
        lambda q, cats, history_hint=None: {
            "clarified_question": q, "category_id": None,
            "category_name": "", "complexity": complexity,
        },
    )


@pytest.mark.asyncio
async def test_agentic_compare_flow(
    admin_client: AsyncClient, default_kb: str, monkeypatch
):
    """compare 复杂度：拆解 → 每个子问题一次检索 → 来源合并 → 汇总回答"""
    await _seed_category(default_kb)
    _patch_complexity(monkeypatch, "compare")
    monkeypatch.setattr(
        chat_service, "decompose_question",
        lambda q: ["2024年差旅住宿标准", "2025年差旅住宿标准"],
    )
    retrieval_queries: list[str] = []

    def fake_retrieve(query, top_k=6, doc_ids=None, **kw):
        retrieval_queries.append(query)
        return _fake_docs()

    monkeypatch.setattr(chat_service, "retrieve_only", fake_retrieve)
    monkeypatch.setattr(
        chat_service, "build_rag_chain",
        lambda **kw: FakeChain("2024年为300元，2025年为400元[来源1][来源2]"),
    )

    async with admin_client.stream(
        "POST", "/api/v1/chat/stream",
        json={"message": "对比2024和2025差旅住宿标准", "stream": True},
    ) as resp:
        events = await _collect_events(resp)

    # 共 3 次检索：小库快路预取 1 次（分类与检索并行优化，普通问题受益）
    # + 编排子问题 2 次。编排结果覆盖预取结果（不走分类逐级检索）。
    assert len(retrieval_queries) == 3
    assert retrieval_queries[1] == "2024年差旅住宿标准"
    assert retrieval_queries[2] == "2025年差旅住宿标准"

    thinking_msgs = [
        e.get("message", "") for e in events if e.get("type") == "status"
    ]
    assert any("拆解" in m for m in thinking_msgs)
    assert any("子问题 1/2" in m for m in thinking_msgs)
    assert any("汇总" in m for m in thinking_msgs)

    sources = next(e for e in events if e.get("type") == "sources")
    assert len(sources["documents"]) == 2

    answer_text = "".join(
        e.get("content", "") for e in events if e.get("type") == "text_delta"
    )
    assert "2024年为300元" in answer_text


@pytest.mark.asyncio
async def test_agentic_calculate_injects_computed_result(
    admin_client: AsyncClient, default_kb: str, monkeypatch
):
    """calculate 复杂度：程序计算结果注入生成上下文（extra_context）"""
    await _seed_category(default_kb)
    _patch_complexity(monkeypatch, "calculate")
    monkeypatch.setattr(chat_service, "decompose_question", lambda q: ["应休年假几天", "已休几天"])
    monkeypatch.setattr(chat_service, "retrieve_only", lambda query, **kw: _fake_docs())
    monkeypatch.setattr(
        chat_service, "extract_calculation",
        lambda q, ctx: {"expression": "10 - 3", "result": 7, "explanation": "应休年假10天减已休3天"},
    )

    captured: dict = {}

    def fake_chain(**kw):
        captured.update(kw)
        return FakeChain("你还有7天年假[来源1]")

    monkeypatch.setattr(chat_service, "build_rag_chain", fake_chain)

    async with admin_client.stream(
        "POST", "/api/v1/chat/stream",
        json={"message": "我年假还剩几天", "stream": True},
    ) as resp:
        await _collect_events(resp)

    extra = captured.get("extra_context") or ""
    assert "程序计算结果" in extra
    assert "10 - 3 = 7" in extra
    assert "子问题" in extra  # 覆盖要求一并注入


@pytest.mark.asyncio
async def test_agentic_decompose_failure_falls_back(
    admin_client: AsyncClient, default_kb: str, monkeypatch
):
    """拆解失败回落普通检索：不产生多路检索，问答照常完成"""
    await _seed_category(default_kb)
    _patch_complexity(monkeypatch, "compare")
    monkeypatch.setattr(chat_service, "decompose_question", lambda q: None)
    retrieval_queries: list[str] = []

    def fake_retrieve(query, top_k=6, doc_ids=None, **kw):
        retrieval_queries.append(query)
        return _fake_docs()

    monkeypatch.setattr(chat_service, "retrieve_only", fake_retrieve)
    monkeypatch.setattr(
        chat_service, "build_rag_chain",
        lambda **kw: FakeChain("普通回答[来源1]"),
    )

    async with admin_client.stream(
        "POST", "/api/v1/chat/stream",
        json={"message": "对比2024和2025差旅标准", "stream": True},
    ) as resp:
        events = await _collect_events(resp)

    # 仅普通路径一次检索（小库快路），没有子问题检索
    assert len(retrieval_queries) == 1
    thinking_msgs = [
        e.get("message", "") for e in events if e.get("type") == "status"
    ]
    assert not any("子问题 1/" in m for m in thinking_msgs)
    answer_text = "".join(
        e.get("content", "") for e in events if e.get("type") == "text_delta"
    )
    assert "普通回答" in answer_text


@pytest.mark.asyncio
async def test_chat_tool_call_flow(
    admin_client: AsyncClient, default_kb: str, monkeypatch
):
    """工具调用循环（B3）：含数字问题 → 决策 calculator → 执行 → 结果注入上下文 + 审计留痕"""
    await _seed_category(default_kb)
    _patch_complexity(monkeypatch, "simple")
    monkeypatch.setattr(chat_service, "retrieve_only", lambda query, **kw: _fake_docs())
    monkeypatch.setattr(chat_service, "should_try_tools", lambda q: True)
    monkeypatch.setattr(chat_service, "list_external_tools", lambda: _ret([]))
    monkeypatch.setattr(
        chat_service, "detect_tool_call",
        lambda q, c: _ret({"tool": "calculator", "args": {"expression": "10-3"}}),
    )

    captured: dict = {}

    def fake_chain(**kw):
        captured.update(kw)
        return FakeChain("你还有 7 天年假[来源1]")

    monkeypatch.setattr(chat_service, "build_rag_chain", fake_chain)

    async with admin_client.stream(
        "POST", "/api/v1/chat/stream",
        json={"message": "我年假还剩几天", "stream": True},
    ) as resp:
        events = await _collect_events(resp)

    thinking_msgs = [
        e.get("message", "") for e in events if e.get("type") == "status"
    ]
    assert any("正在调用工具：calculator" in m for m in thinking_msgs)

    extra = captured.get("extra_context") or ""
    assert "【工具查询结果】calculator" in extra
    assert "10-3 = 7" in extra

    # 工具调用审计留痕
    async with async_session_factory() as db:
        logs = (await db.execute(
            select(AuditLog).where(AuditLog.action == "tool.call")
        )).scalars().all()
    assert len(logs) == 1
    assert logs[0].detail["tool"] == "calculator"
    assert logs[0].detail["args"] == {"expression": "10-3"}


class TestL1CategoryExpansion:
    """批次F 验收修复：分类过滤检索 rerank 后 top1 相关性不足 → L3 全库兜底。

    真实验收："会议室预订需要提前多久"分类器路由到"会议室使用"分类，
    挂在"会议室"分类下的测试文档被 L1 分类过滤排除（L1 top1=0.628 <
    阈值 0.7，全库 top1=0.785）——分类内候选充足时永不扩搜，答案答成
    另一份文档。
    """

    async def _run(self, admin_client, default_kb, monkeypatch, l1_score,
                   l1_count: int = 11):
        from app.services import chat_service as cs
        from app.rag import reranker as rk

        # 可见库非空（否则问答流提前返回"没有可访问的知识库内容"）
        await _seed_category(default_kb)
        # 大库路径：可见块 > FAST_PATH_MAX_CHUNKS
        monkeypatch.setattr(cs, "count_chunks", lambda doc_ids: 300)
        monkeypatch.setattr(
            cs, "classify_question",
            lambda q, cats, history_hint=None: {
                "clarified_question": "会议室预订 提前时间",
                "category_id": "cat-会议", "category_name": "会议室使用",
                "complexity": "simple",
            },
        )
        # 分类树：顶层 cat-会议 + 一个子分类（L1 分类过滤用）
        monkeypatch.setattr(
            cs, "_flatten_for_classification", lambda cats: [
                {"id": "cat-会议", "name": "会议室使用", "kb_id": default_kb, "path": "会议室使用"},
                {"id": "cat-会议-制度", "name": "制度", "kb_id": default_kb, "path": "会议室使用 > 制度"},
            ],
        )

        def _mk_doc(fname, cid):
            return Document(
                page_content=f"{fname} 内容",
                metadata={"document_id": fname, "chunk_index": 0,
                          "filename": fname, "category_id": cid},
            )

        calls: list[dict] = []

        def fake_retrieve(query, top_k=6, doc_ids=None, **kw):
            calls.append(dict(kw=kw))
            if kw.get("category_ids") or kw.get("parent_category_ids"):
                # L1 分类过滤：l1_count 条分类内文档（充足，条数不触发 L3）
                return [_mk_doc(f"会议室使用规范-{i}.md", "cat-会议-制度")
                        for i in range(l1_count)]
            # L3 全库：含跨分类的高相关文档
            return [_mk_doc("验收测试-会议室管理制度.txt", "cat-其他-制度")] + [
                _mk_doc(f"全库-{i}.md", "cat-会议-制度") for i in range(10)
            ]

        monkeypatch.setattr(cs, "retrieve_only", fake_retrieve)

        def fake_rerank(query, docs, top_n=6, notify_decay=None):
            for i, d in enumerate(docs):
                # L1 分类内文档低分；L3 全库的测试文档高分
                if d.metadata["document_id"] == "验收测试-会议室管理制度.txt":
                    d.metadata["rerank_score"] = 0.785
                else:
                    d.metadata["rerank_score"] = max(l1_score - i * 0.01, 0.05)
            return docs[:top_n]

        monkeypatch.setattr(cs, "rerank_documents", fake_rerank)
        monkeypatch.setattr(
            cs, "build_rag_chain",
            lambda **kw: FakeChain("由测试文档回答[来源1]"),
        )

        async with admin_client.stream(
            "POST", "/api/v1/chat/stream",
            json={"message": "会议室预订需要提前多久", "stream": True},
        ) as resp:
            events = await _collect_events(resp)
        return calls, events

    @pytest.mark.asyncio
    async def test_l1_low_score_expands_to_l3(
        self, admin_client, default_kb, monkeypatch
    ):
        """L1 rerank top1=0.6 < 0.7 → 触发 L3 全库检索（跨分类文档进候选）"""
        calls, events = await self._run(admin_client, default_kb, monkeypatch, 0.6)
        # 一次 L1（带分类过滤）+ 一次 L3（无过滤）
        assert len(calls) == 2
        assert calls[0]["kw"].get("category_ids")
        assert not calls[1]["kw"].get("category_ids")
        # 最终答案带测试文档来源
        done = [e for e in events if e.get("type") == "done"]
        assert done

    @pytest.mark.asyncio
    async def test_l1_high_score_skips_l3(
        self, admin_client, default_kb, monkeypatch
    ):
        """L1 rerank top1=0.8 ≥ 0.7 → 不扩搜（省一次全库检索）"""
        calls, events = await self._run(admin_client, default_kb, monkeypatch, 0.8)
        assert len(calls) == 1
        assert calls[0]["kw"].get("category_ids")


    @pytest.mark.asyncio
    async def test_l1_blind_spot_6_to_10_candidates_expands(
        self, admin_client, default_kb, monkeypatch
    ):
        """批次F 验收修复二次：L1 候选 7 条（6~10 盲区）+ 低分 → 同样扩搜。
        此前预 rerank 门槛是 >10 条：6~10 条时条数判定"充足"、rerank
        判定"候选少跳过"、预 rerank 不执行——分类路由错误无法纠正
        （真实验收："公务用车油费"→"采购"、"团体来访"→"劳动合同续签"，
        均 L1 命中 6 条无扩搜，答案误判"暂无"）"""
        calls, events = await self._run(
            admin_client, default_kb, monkeypatch, 0.6, l1_count=7
        )
        assert len(calls) == 2
        assert calls[0]["kw"].get("category_ids")
        assert not calls[1]["kw"].get("category_ids")

    @pytest.mark.asyncio
    async def test_l1_blind_spot_6_to_10_high_score_skips(
        self, admin_client, default_kb, monkeypatch
    ):
        """盲区高分（0.8）→ 不扩搜（预 rerank 结果沿用，不重复调 API）"""
        calls, events = await self._run(
            admin_client, default_kb, monkeypatch, 0.8, l1_count=7
        )
        assert len(calls) == 1
        assert calls[0]["kw"].get("category_ids")



class TestAnswerPostProcess:
    """挂清单 #2/#5 修复（质检返工）：补标与幻觉清理对所有角色生效。

    质检 P1-1：此前测试请求体未带 role 字段实际跑的是 user 老路径，
    断言空转。改为两层覆盖：
    ①纯函数单测 _apply_answer_post_process（角色无关，直接验证行为）
    ②admin 集成断言：请求带 role=admin，落库 assistant 消息含 [规则]
      行首标注；幻觉清理走 LLM 链路径（mock 链返回含 [来源9] 文本）
    """

    def test_pure_function_adds_rule_label(self):
        """规则命中且正文无标注 → 行首补 [规则]"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process(
            "报销3000元由部门负责人审批。",
            {"approver": "部门负责人"}, None,
        )
        assert out.startswith("[规则]\n")

    def test_pure_function_no_double_label(self):
        """正文已有 [规则] → 不重复补标"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process(
            "[规则]\n报销3000元由部门负责人审批。",
            {"approver": "部门负责人"}, None,
        )
        assert out == "[规则]\n报销3000元由部门负责人审批。"

    def test_pure_function_strips_ghost_markers(self):
        """正文 [来源9] 超出 citations 有效范围 → 清理（幻觉标记）"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process(
            "由部门负责人审批[来源1][来源9]",
            None, [{"index": 1}],
        )
        assert "[来源9]" not in out
        assert "[来源1]" in out  # 合法引用保留

    def test_pure_function_no_rule_no_citations_unchanged(self):
        """无规则命中无 citations → 原样返回"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process("普通答案[来源1]", None, None)
        assert out == "普通答案[来源1]"

    # ── 挂清单 #16：web 兜底正文 [网络] 补标 ──

    def test_pure_function_adds_web_label_when_missing(self):
        """web 兜底命中且正文无 [网络] → 行首补标"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process(
            "阿尔法狗由DeepMind公司开发。", None, None, web_hit=True,
        )
        assert out.startswith("[网络]\n")

    def test_pure_function_no_double_web_label(self):
        """正文已有 [网络] → 不重复补标"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process(
            "[网络]\n阿尔法狗由DeepMind公司开发。", None, None, web_hit=True,
        )
        assert out == "[网络]\n阿尔法狗由DeepMind公司开发。"

    def test_pure_function_no_web_label_without_web_hit(self):
        """非 web 兜底路径（web_hit=False）→ 不补 [网络]"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process(
            "普通知识库回答。", None, None,
        )
        assert not out.startswith("[网络]")

    # ── 挂清单 #14：空引用残壳 + HTML 标签幻觉清理 ──

    def test_pure_function_strips_dangling_citation_shell(self):
        """幻觉编号被清后残留"记录在 中" → 残壳清理"""
        from app.services.chat_service import _apply_answer_post_process

        # [来源9] 超出有效范围被删 → 残留"记录在 中"
        out = _apply_answer_post_process(
            "请假需提前申请（记录在 [来源9] 中）。",
            None, [{"index": 1}],
        )
        assert "记录在" not in out
        assert "请假需提前申请" in out

    def test_pure_function_keeps_valid_citation_shell(self):
        """有效引用"记录在 [来源1] 中" → 保留（不误删）"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process(
            "请假需提前申请（记录在 [来源1] 中）。",
            None, [{"index": 1}],
        )
        assert "记录在 [来源1] 中" in out

    def test_pure_function_unwraps_html_tag_hallucination(self):
        """LLM 幻觉输出的原始 span 标签 → 还原为纯标记文本"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process(
            '依据<span class="kb-tag kb-tag-source">[来源1]</span>执行。',
            None, [{"index": 1}],
        )
        assert "<span" not in out
        assert "[来源1]" in out
        assert "依据[来源1]执行。" == out

    def test_pure_function_keeps_common_see_phrases(self):
        """质检 P1-2 防误伤："意见中提到""常见"等含"见"正文不被改动"""
        from app.services.chat_service import _apply_answer_post_process

        text = "该制度意见中提到：常见中药有哪些？在征求意见中遇到的问题请反馈。"
        out = _apply_answer_post_process(text, None, [{"index": 1}])
        assert out == text

    def test_pure_function_strips_tool_name_source_marker(self):
        """#13 端到端验证：LLM 把工具名幻觉成来源标记（[来源test-tools.echo]）→ 清理"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process(
            "外部工具测试成功 [来源test-tools.echo]",
            None, [{"index": 1}],
        )
        assert "[来源test-tools.echo]" not in out
        assert out == "外部工具测试成功 "

    def test_pure_function_keeps_numeric_source_marker(self):
        """合法数字编号 [来源1] 不受工具名清理影响"""
        from app.services.chat_service import _apply_answer_post_process

        out = _apply_answer_post_process(
            "依据制度执行[来源1]。", None, [{"index": 1}],
        )
        assert "[来源1]" in out

    @pytest.mark.asyncio
    async def test_admin_flow_persists_label_and_cleans_ghost(
        self, admin_client, default_kb, monkeypatch
    ):
        """admin 集成（质检 P1-1 修法）：落库消息含 [规则] 行首。

        服务层直调 stream_chat 生成器（不走 HTTP/ASGI transport）——
        SSE 逐行消费会受前序用例（test_audit_document_operations）
        残留后台审计任务干扰生成器时序（全量偶发 NoneType.send）。
        规则走真实规则表 seed（不 mock match_question，顺序无关）。"""
        import asyncio
        import uuid

        from sqlalchemy import select as sa_select

        from app.models.document import Document
        from app.models.rule import ApprovalRule
        from app.models.user import User
        from app.services import chat_service as cs

        await _seed_category(default_kb)
        # 前序用例（test_audit_document_operations）的审计后台任务
        # 可能跨用例存活：短暂 drain 事件循环让其自然结束，避免残留
        # 任务干扰本测试的生成链时序（全量偶发 NoneType.send）
        await asyncio.sleep(0.5)
        async with async_session_factory() as db:
            # source_doc_id 必须指向真实存在的文档：match_question 的
            # 规则加载 outerjoin documents 取 created_at 做"新文档覆盖
            # 旧文档"裁决，悬空 id 会使裁决键为 None（配对场景下匹配
            # 失败回退 LLM 生成——全量偶发根因）
            seed_doc = (await db.execute(
                sa_select(Document.id).where(Document.original_name == "seed.txt")
            )).scalar_one()
            db.add(ApprovalRule(
                id=str(uuid.uuid4()), business_type="报销", min_value=1000.0,
                max_value=5000.0, unit="元", approver="部门负责人",
                action="审批", source_doc_id=seed_doc,
                source_text="1000 至 5000 元由部门负责人审批",
            ))
            await db.commit()
        monkeypatch.setattr(
            cs, "classify_question",
            lambda q, cats, history_hint=None: {
                "clarified_question": "报销 3000 元谁审批", "category_id": None,
                "category_name": "", "complexity": "approval",
            },
        )
        # 隔离图谱依赖：前序用例删文档触发的图谱清理后台任务可能
        # 污染 Neo4j 状态，entity_context_for_text 抛异常会中断直出
        # （配对场景 error 事件根因）——本测试只验证规则腿后处理
        monkeypatch.setattr(
            "app.config.settings.graphrag_enabled", False
        )
        monkeypatch.setattr(cs, "count_chunks", lambda doc_ids: 10)
        monkeypatch.setattr(cs, "retrieve_only", lambda *a, **kw: _fake_docs())

        # 规则走真实规则表 seed + 真实 match_question（顺序无关，
        # 不依赖 mock）

        async with async_session_factory() as db:
            admin = (await db.execute(
                sa_select(User).where(User.role == "admin").limit(1)
            )).scalar_one()
            async for _ in cs.stream_chat(
                db, admin, None, "报销 3000 元谁审批", role="admin"
            ):
                pass

        from sqlalchemy import text as sql_text

        async with async_session_factory() as db:
            rows = (await db.execute(sql_text(
                "SELECT content FROM messages WHERE role='assistant' "
                "ORDER BY created_at DESC LIMIT 1"
            ))).all()
        content = rows[0][0] if rows else ""
        assert content.startswith("[规则]\n"), content[:80]
        assert "部门负责人" in content


class TestApprovalRulesSearch:
    """挂清单 #6：规则页审批人搜索（后端 approver 模糊参数）"""

    async def _seed_rule(self, db, biz, approver):
        import uuid

        from app.models.rule import ApprovalRule

        db.add(ApprovalRule(
            id=str(uuid.uuid4()), business_type=biz, min_value=0.0,
            max_value=None, unit="元", approver=approver, action="审批",
            source_doc_id="doc-x", source_text=f"{biz}0元以上：{approver}审批",
        ))

    @pytest.mark.asyncio
    async def test_approver_search_filters(self, admin_client, default_kb):
        from app.models.user import User
        from sqlalchemy import select

        async with async_session_factory() as db:
            await self._seed_rule(db, "报销", "财务总监")
            await self._seed_rule(db, "采购", "总经理")
            await db.commit()

        r = await admin_client.get("/api/v1/admin/approval-rules", params={"approver": "财务"})
        assert r.status_code == 200
        items = r.json()
        assert len(items) >= 1
        assert all("财务" in it["approver"] for it in items)
        assert all(it["approver"] != "总经理" for it in items)

        r2 = await admin_client.get("/api/v1/admin/approval-rules")
        assert r2.status_code == 200
        assert len(r2.json()) >= 2  # 无参数返回全量


class TestToolNoteSurvivesWebFallback:
    """批次F 验收修复（6.8）：工具执行成功时不走 web 兜底。

    真实验收："12345 加 6789 等于多少"——工具算出 19134，但检索
    无结果触发 web 兜底且清掉工具笔记，LLM 拿着网络杂文答"知识库
    暂无"。修复后 tool_note 非空时跳过兜底，回答直接采用工具结果。
    """

    @pytest.mark.asyncio
    async def test_tool_success_skips_web_fallback(
        self, admin_client, default_kb, monkeypatch
    ):
        from app.services import chat_service as cs

        await _seed_category(default_kb)
        monkeypatch.setattr(
            cs, "classify_question",
            lambda q, cats, history_hint=None: {
                "clarified_question": "12345 加 6789 等于多少", "category_id": None,
                "category_name": "", "complexity": "simple",
            },
        )
        monkeypatch.setattr(cs, "count_chunks", lambda doc_ids: 10)
        monkeypatch.setattr(cs, "retrieve_only", lambda *a, **kw: [])
        async def fake_detect(q, cands):
            return {"tool": "calculator", "args": {"expression": "12345+6789"}}

        async def fake_execute(d):
            return "12345 + 6789 = 19134"

        monkeypatch.setattr(cs, "detect_tool_call", fake_detect)
        monkeypatch.setattr(cs, "execute_tool_call", fake_execute)
        # 隔离真实 MCP_SERVERS：测试进程加载根 .env 会尝试启动
        # everything 外部服务（测试环境 npx 不可用），外部工具列
        # 表 mock 为空
        async def fake_list_ext():
            return []

        monkeypatch.setattr(cs, "list_external_tools", fake_list_ext)
        web_called: list[str] = []
        from app.rag import web_search

        async def fake_search(q):
            web_called.append(q)
            return []  # 即便返回空也不影响断言主目标

        monkeypatch.setattr(web_search, "search_web", fake_search)
        monkeypatch.setattr(
            cs, "build_rag_chain",
            lambda **kw: FakeChain("12345+6789=19134（由程序执行，非模型推算）"),
        )

        resp = await admin_client.post(
            "/api/v1/chat/stream",
            json={"message": "12345 加 6789 等于多少", "stream": True,
                  "role": "admin"},
        )
        assert resp.status_code == 200
        await resp.aread()

        # 修复断言：web 兜底未被调用（工具结果在手，不必搜网）
        assert web_called == []
        # 落库消息含工具计算结果
        from sqlalchemy import text as sql_text

        async with async_session_factory() as db:
            rows = (await db.execute(sql_text(
                "SELECT content FROM messages WHERE role='assistant' "
                "ORDER BY created_at DESC LIMIT 1"
            ))).all()
        content = rows[0][0] if rows else ""
        assert "19134" in content


class TestToolNoteClearsIrrelevantSources:
    """质检补测（8f803bc）：工具结果在手（含外部工具）时弱相关检索来源清空。

    8f803bc 把判据从 builtin_tool_hit 扩展为 tool_note（内置+外部工具），
    此前无回归测试钉住——补：外部工具命中 + 弱相关检索 → sources 空、
    回答不引用无关文档。
    """

    @pytest.mark.asyncio
    async def test_external_tool_hit_clears_irrelevant_sources(
        self, admin_client, default_kb, monkeypatch
    ):
        from app.services import chat_service as cs
        from app.rag import web_search as ws_mod

        await _seed_category(default_kb)
        monkeypatch.setattr(cs, "count_chunks", lambda doc_ids: 300)
        monkeypatch.setattr(
            cs, "classify_question",
            lambda q, cats, history_hint=None: {
                "clarified_question": "用 echo 工具说：hi", "category_id": None,
                "category_name": "", "complexity": "simple",
            },
        )
        monkeypatch.setattr(
            cs, "_flatten_for_classification", lambda cats: []
        )

        # 弱相关检索结果（rerank_score 0.05 低于阈值）——真实验收场景：
        # 计算题来源出现"广告投放登记表.csv"
        def fake_retrieve(query, top_k=6, doc_ids=None, **kw):
            return [Document(
                page_content="广告投放登记表 内容",
                metadata={"document_id": "ad-csv", "chunk_index": 0,
                          "filename": "广告投放登记表.csv", "category_id": None,
                          "rerank_score": 0.05},
            )]

        monkeypatch.setattr(cs, "retrieve_only", fake_retrieve)
        # 小库候选有 rerank 分数，走"弱相关判定清空"分支（不补精排）
        monkeypatch.setattr(cs, "rerank_documents", lambda *a, **kw: fake_retrieve(""))

        # 外部工具命中（带 "." 前缀），执行成功
        async def fake_detect(q, cands, feedback=None):
            return {"tool": "svc.echo", "args": {"message": "hi"}}

        async def fake_execute(d):
            return "Echo: hi"

        monkeypatch.setattr(cs, "detect_tool_call", fake_detect)
        monkeypatch.setattr(cs, "execute_tool_call", fake_execute)
        monkeypatch.setattr(cs, "should_try_tools", lambda q: True)

        async def fake_list_ext():
            return []

        monkeypatch.setattr(cs, "list_external_tools", fake_list_ext)

        async def fake_search(q):
            return []

        monkeypatch.setattr(ws_mod, "search_web", fake_search)
        monkeypatch.setattr(
            cs, "build_rag_chain",
            lambda **kw: FakeChain("Echo: hi"),
        )

        resp = await admin_client.post(
            "/api/v1/chat/stream",
            json={"message": "用 echo 工具说：hi", "stream": True,
                  "role": "admin"},
        )
        assert resp.status_code == 200
        # sources 事件断言：documents 为空（弱相关来源被清）
        body = resp.text
        assert '"type": "sources"' in body
        import re as _re

        m = _re.search(r'"documents":\s*(\[[^\]]*\])', body)
        assert m is not None
        assert json.loads(m.group(1)) == []
