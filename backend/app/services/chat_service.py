"""对话服务 - RAG 问答编排"""
import asyncio
import json
import re
import time

from app.utils.time import tz_now
from typing import AsyncGenerator

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from loguru import logger

from langchain_core.documents import Document as LangchainDocument
from app.config import settings
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.message import Message
from app.models.user import User
from app.rag.chain import build_rag_chain, retrieve_only
from app.rag.retriever import expand_with_chapter_siblings
from app.rag.classifier import classify_question
from app.rag.llm import TokenUsageHandler
from app.rag.agentic import (
    decompose_question, extract_calculation, format_calculation_note,
    merge_documents, MAX_CONTEXT_DOCS,
)
from app.rag.tools import (
    BUILTIN_TOOLS, should_try_tools, list_external_tools,
    detect_tool_call, execute_tool_call, format_tool_note,
    detect_dangerous_request, refresh_mcp_config,
)
from app.rag.reranker import rerank_documents
from app.rag.retriever import count_chunks
from app.services import audit_service, conversation_service, document_service, kb_service

# 员工端回答字数上限（企业知识库改造阶段2：可配置，默认 300）
USER_ANSWER_MAX_CHARS = settings.user_answer_max_chars
USER_ANSWER_MAX_OVER = 20    # 截断时允许补完当前句的超限窗口（+20字）
USER_TAIL_WINDOW = 80        # 截断兜底时允许句子收尾的缓冲窗口

ANSWER_K = 6   # 最终喂给 LLM 的文档条数（上下文容量与成本约束）
RECALL_K = 20  # 各检索级的候选召回条数（P3-3：粗召回后经 rerank 精排取 ANSWER_K 条）
RERANK_MIN_CANDIDATES = 10  # 延迟优化 B：候选不超过此数时跳过 rerank（重排收益趋近于零）
FAST_PATH_MAX_CHUNKS = 200  # 延迟优化 A+C：分块总数不超过此值时走"小库快路"（分类与全库检索并行）


def _order_by_category_priority(
    docs: list, category_ids: list[str] | None, limit: int
) -> tuple[list, bool]:
    """小库快路后置挑拣：分类内文档优先，其余全库文档补足至 limit。

    返回 (排序后文档列表, 是否存在分类内命中)。
    P3-1 语义一致：分类内不足时，无分类/其他分类文档也会被纳入候选。
    """
    cat_set = set(category_ids or [])

    def _key(d) -> tuple:
        meta = d.metadata or {}
        did, idx = str(meta.get("document_id") or ""), str(meta.get("chunk_index") or "")
        if did or idx:
            return (did, idx)
        return ("content", d.page_content)  # 兜底：元数据缺失时按内容去重

    seen: set = set()
    ordered: list = []
    category_hit = False
    if cat_set:
        for d in docs:
            if (d.metadata or {}).get("category_id") in cat_set:
                category_hit = True
                k = _key(d)
                if k not in seen:
                    seen.add(k)
                    ordered.append(d)
    for d in docs:
        k = _key(d)
        if k not in seen:
            seen.add(k)
            ordered.append(d)
    return ordered[:limit], category_hit


# 企业知识库改造阶段2：电商客服的代码兜底追问（尺码/售后/指代商品）已随客服功能剥离，
# 信息不足时的追问交给提示词规则与模型处理。


async def build_notify_decay(db, doc_ids: list[str]) -> dict[str, float]:
    """通知/公告类文档时效衰减系数（验收批次1·8i；批次语料增强抽为公共函数——
    评估口径与生产链路共用同一衰减计算）。

    文件名含"通知/公告"的文档按发布天数算衰减（90 天全权重/365 天 0.7/
    更旧 0.4）；系数在 RRF 融合与 rerank 精排两层都生效（纯语义排序会把
    内容几乎相同的跨年度通知排乱，衰减必须介入精排层）。
    """
    notify_rows = (await db.execute(
        select(Document.id, Document.created_at).where(
            Document.id.in_(doc_ids),
            Document.original_name.like("%通知%") | Document.original_name.like("%公告%"),
        )
    )).all()
    if not notify_rows:
        return {}
    from datetime import datetime as _dt

    from app.rag.retriever import notify_decay_factor

    _now = _dt.now()
    return {
        rid: notify_decay_factor((_now - created_at).days)
        for rid, created_at in notify_rows
    }


def _with_history_context(query: str, chat_history: list[dict] | None) -> str:
    """多轮检索：只在需要上文时才把最近一轮对话并入检索词。

    - 指代类追问（"那女生呢""这个能洗吗"）→ 注入上一轮用户+客服的话
      （各截 100 字），命中上文提到的实体/商品；
    - 短追问（≤6 字，如"怎么申请""哪些不辣"）→ 同样注入（缺主语，
      上文是唯一线索）；
    - 完整新问题（≥7 字且无指代词）→ 不注入——真实验收：先问组织架构
      再问"IT 故障怎么报修"（8 字自足问题），客服答的组织架构内容把
      IT 报障块挤出检索候选，答案误判"知识库暂无此信息"；上一轮答案
      对新问题纯属噪声。
    """
    if not chat_history:
        return query
    stripped = query.strip()
    # 指代词判定前先剔除"其他/其它"（"其他部门的考勤怎么算"不是指代）
    _referential = any(
        w in stripped for w in ("这个", "那个", "这些", "那些", "上述", "前面",
                                "刚才", "上面", "之前", "上一条", "以上")
    )
    if not _referential:
        bare = stripped.replace("其他", "").replace("其它", "")
        _referential = any(
            w in bare for w in ("他", "她", "它", "他们", "她们", "它们")
        )
    if len(stripped) >= 7 and not _referential:
        return query
    last_user = next(
        (m["content"] for m in reversed(chat_history) if m["role"] == "user"), None
    )
    last_assistant = next(
        (m["content"] for m in reversed(chat_history) if m["role"] == "assistant"), None
    )
    if not last_user and not last_assistant:
        return query
    parts = []
    if last_user:
        parts.append(f"用户刚才问：{last_user[:100]}")
    if last_assistant:
        parts.append(f"客服刚才说：{last_assistant[:100]}")
    return f"{query}（上下文：{'；'.join(parts)}）"


def _retrieval_irrelevant(docs: list, complexity: str | None) -> bool:
    """低相关判定（批次语料增强二期）：top1 rerank 分低于阈值视为
    "知识库答不了" → web 搜索兜底。

    背景：检索层无分数过滤（向量腿永远返回满额最近邻），仅"无结果"
    判定让 11f 兜底在真实环境永远点不了火（E2E 实测发现）。

    判定规则：
    - 规则/图谱腿复杂度（approval/relation）不参与——确定性通道
      （审批题检索分数天然低，如"报销 3000 元谁审批"top1 仅 0.206，
      但规则腿能确定性回答，不该搜网）
    - 无 rerank_score（未走 rerank 的小候选路径或 rerank 失败降级路径）
      返回 False 保守不触发
    - 阈值 settings.web_search_min_score（默认 0.15，实测标定依据见
      config：正常题 0.206~0.838，知识库外题 0.006~0.128）
    """
    if complexity in ("relation", "approval"):
        return False
    if not docs:
        return False
    score = docs[0].metadata.get("rerank_score")
    return score is not None and score < settings.web_search_min_score


def resolve_effective_role(requested_role: str, user_role: str) -> str:
    """校验请求角色：非 admin 用户请求 admin 时降级为 user"""
    if requested_role == "admin" and user_role != "admin":
        return "user"
    return requested_role


def _truncate_answer(text: str, max_len: int, over: int = USER_ANSWER_MAX_OVER) -> tuple[str, bool]:
    """
    按句界软截断（补句语义）：
    1. 未超 max_len → 原样返回
    2. 在 [max_len, max_len+over] 窗口内找第一个句末标点（。！？；\\n），
       命中则截到该句号（补完当前句，允许小幅超限）
    3. 窗口内无句界 → 回退到 max_len 内最后一个句界（位置 ≥ max_len*0.5 才有效）
    4. 都无 → 硬切 max_len 并补 "…"
    返回 (截断后文本, 是否发生截断)。
    """
    if len(text) <= max_len:
        return text, False
    window = text[: max_len + over]
    # 2) 补句：max_len 附近起的第一个句末边界
    for i in range(max_len - 1, len(window)):
        if window[i] in ("。", "！", "？", "；", "\n"):
            return window[: i + 1].rstrip(), True
    # 3) 回退：max_len 内最后一个有效句界
    head = text[:max_len]
    best = -1
    for punc in ("。", "！", "？", "；", "\n"):
        pos = head.rfind(punc)
        if pos > best:
            best = pos
    if best >= int(max_len * 0.5):  # 边界太靠前则视为无有效句界，硬切
        return head[: best + 1].rstrip(), True
    # 4) 硬切
    return head.rstrip() + "…", True


def _trim_dangling_citation(text: str) -> str:
    """清理截断在 [来源N] 标记内部切断的残缺标记（如"[来源"、"[来源1"）。

    员工端保留引用后，句界截断可能恰好切进标记中间，落库的残缺标记
    会让前端引用匹配失败；这里把结尾的残缺标记剔除。
    """
    return re.sub(r"\[来源\d{0,2}$", "", text).rstrip()


def _apply_answer_post_process(
    full_answer: str, rule_hit: dict | None, citations: list[dict] | None,
    web_hit: bool = False,
) -> str:
    """批次F 验收修复（挂清单 #2/#5/#14/#16 抽出）：回答后处理对所有角色生效。

    - 规则腿命中但正文无 [规则] 标注时行首补标（主 prompt 的 [来源N]
      引用体系与规则伪块的 [规则] 指令冲突，LLM 自觉不可靠——真实验收：
      三次答案内容正确但标注全缺；行首前置为用户 2026-10-14 拍板）
    - web 兜底命中但正文无 [网络] 标注时行首补标（挂清单 #16——
      LLM 标注不稳定，与 [规则] 补标同族；用户分不清答案来自网络还是库）
    - 清理引用不到真实来源的 [来源N] 残留（模型把"第十四条"误写成
      [来源14] 时不留点不动的假标记；admin 正文曾残留 [来源8][来源9]
      幻觉标记——超出 6 条范围点不到来源）
    - 挂清单 #14：清理无效标记删除后的残壳（"记录在 中"——LLM 写了
      幻觉编号 [来源N]，标记被上一步删掉只剩引导词）；LLM 幻觉输出
      的原始 HTML span 标签还原为纯标记文本（前端 sanitize 虽放行
      span+className，但原始标签照抄进正文属观感瑕疵）
    """
    if rule_hit and "[规则]" not in full_answer:
        full_answer = "[规则]\n" + full_answer
    if web_hit and "[网络]" not in full_answer:
        full_answer = "[网络]\n" + full_answer
    if citations is not None:
        valid = {c["index"] for c in citations}
        full_answer = _remove_invalid_citation_markers(full_answer, valid)
    # #14②：无效标记删除后的引用残壳（"记录在 中"等）。质检 P1-2：
    # 裸"见"分支误伤正文（"意见中提到"→"意提到"、"常见中药"→"常药"）
    # ——只清理明确的引用引导词（双字词，误伤面极小）
    full_answer = re.sub(r"(记录在|详见|参见)\s*中", "", full_answer)
    # 批次F 验收修复（#13 端到端验证）：LLM 把工具名幻觉成来源标记
    # （"[来源test-tools.echo]"——真实数字编号之外的 [来源X] 一律清理；
    # 负向断言只放行纯数字编号）
    full_answer = re.sub(r"\[来源(?![0-9]+\])[^\]]*\]", "", full_answer)
    # #14③：LLM 幻觉输出的原始 HTML 标签还原（含 className 完整形态与
    # 简化形态），未配对残留标签剥除
    full_answer = re.sub(
        r'<span\s+class="kb-tag[^"]*">([^<]*)</span>', r"\1", full_answer
    )
    full_answer = re.sub(r'</?span\b[^>]*>', "", full_answer)
    return full_answer


def _remove_invalid_citation_markers(text: str, valid_indices: set[int]) -> str:
    """验收修复 A5：清理引用不到真实来源的 [来源N] 残留。

    模型可能把文档正文里的条款编号误写成引用编号（如"第十四条"→[来源14]，
    而来源列表最多 6 条）——前端引用标签只按 citations 渲染，正文里
    点不动的无效标记会误导员工。保留有效标记（员工端设计上保留引用可查证）。
    """
    def _repl(m: re.Match) -> str:
        return m.group(0) if int(m.group(1)) in valid_indices else ""

    return re.sub(r"\[来源(\d+)\]", _repl, text)


def _tool_result_param_error(result: str) -> bool:
    """工具结果是否需带反馈重试决策。

    批次F 验收修复（#13 端到端验证）：MCP 服务器参数校验失败
    （"Input validation error"/"Invalid arguments"）与本地计算表达式
    不合法同属参数问题——带错误重试一次决策显著提高小模型遵从率。
    批次F 验收扩展：沙箱"无输出"（LLM 生成列表推导忘了 print——
    真实验收"帮我抽三个随机数"）同样重试，反馈提示补 print。
    """
    lowered = (result or "").lower()
    return ("input validation error" in lowered
            or "invalid arguments" in lowered
            or "expected string" in lowered
            or lowered.startswith("计算失败：表达式不合法")
            or "（代码执行完成，无输出）" in result)


def _category_path_for(metadata: dict, id_to_path: dict[str, str]) -> str:
    """根据文档元数据的分类ID解析分类路径（父分类 > 子分类），缺失时返回"未知分类" """
    cat_id = metadata.get("category_id", "") or ""
    if cat_id in id_to_path:
        return id_to_path[cat_id]
    parent_id = metadata.get("parent_category_id", "") or ""
    if parent_id in id_to_path:
        return id_to_path[parent_id]
    return "未知分类"


# 审批类动作集合（直出拼装分组用；与 matcher.chain 的动作枚举同口径）
_APPROVAL_ACTIONS = ("审批", "审核", "批准", "批复")


def _format_rule_direct_answer(rule_hit: dict, approve: list[dict],
                               others: list[dict]) -> str:
    """批次F 验收修复三次：规则腿确定性直出文本拼装（跳过 LLM 生成）。

    approve=链中除主项外的审批类步骤、others=链中除主项外的备案/报备
    类步骤（调用方已按主项 approver+action 排除主项自身）。拼装顺序：
    低档审批步骤在前 → 主项（若为备案/报备类加"另需"）→ 其余非审批
    步骤以"另需"后缀全量输出。
    质检 P2 修复二次：主项为审批类且链含其他审批类动作（如"5000 以上
    财务总监审批，10000 以上总经理批准"）时主项曾被漏掉——主项无条件
    拼入，保留审批流程顺序（低档在前、主项最高档在后）。
    """
    segments = [f"{s['approver']}{s['action']}" for s in approve]
    main_text = f"{rule_hit['approver']}{rule_hit['action']}"
    if rule_hit["action"] in _APPROVAL_ACTIONS:
        segments.append(main_text)
    else:
        segments.append(f"另需{main_text}")
    segments.extend(f"另需{s['approver']}{s['action']}" for s in others)
    # 挂清单 #1 修复：依据行拼完整链——主项原文 + 链上其他环节原文
    # 并列（此前只列主项，答案提了"另需人事部备案"却无出处可查）；
    # 主项排首，其余为 approve 组在前、others 组在后（组内保链序），
    # 去重防同文重复
    basis_parts: list[str] = [rule_hit["rule_text"]]
    for s in (approve + others):
        rt = s.get("rule_text", "")
        if rt and rt not in basis_parts:
            basis_parts.append(rt)
    return (
        f"{rule_hit['business_type']}{rule_hit['value']:g}"
        f"{rule_hit['unit']}由{'，'.join(segments)}。"
        f"\n依据：{'；'.join(basis_parts)}。"
    )


async def stream_chat(
    db: AsyncSession,
    user: User,
    conversation_id: str | None,
    message_text: str,
    role: str = "user",
    trace_id: str | None = None,
    external_history: list[dict] | None = None,
    capture_usage: bool = False,
    kb_restrict: list[str] | None = None,
) -> AsyncGenerator[str, None]:
    """
    流式 RAG 问答
    生成 SSE 事件: thinking → sources → token... → done
    trace_id（批次4）：请求阶段捕获的 trace 标识（SSE 生成器在请求
    span 上下文外执行，done 事件靠此桥接）
    external_history（批次A·11e）：OpenAI 兼容门 messages 历史注入
    capture_usage（批次A·11e）：普通角色也捕获真实 token 用量
    kb_restrict（批次F 挂清单 #12）：接入凭证库白名单，透传 _stream_answer
    """
    start_time = time.time()

    # 1. 获取或创建会话
    conversation = await _get_or_create_conversation(db, user.id, conversation_id, message_text)

    # 2. 保存用户消息
    user_message = Message(
        conversation_id=conversation.id,
        role="user",
        content=message_text,
        created_at=tz_now(),  # 人工路径消息显式时间（微秒），保证同秒消息顺序稳定
    )
    db.add(user_message)
    await db.flush()

    # 用户消息计数统一在此计入（随后主路径只计 AI 回复 +1）。
    # 好处：任何环节异常/回滚都不会造成 message_count 与实际行数漂移；
    # SQL 原子自增（上线加固：Python 读-改-写在并发下会丢更新）
    await conversation_service.bump_message_count(db, conversation.id, 1)

    # ── 主路径（AI 回答）：先把用户消息落库再流式 ──
    # 修复：此前整个流期间不提交事务，靠 get_db 收尾提交；客户端中途断开时
    # CancelledError（BaseException）绕过 get_db 的 except Exception，
    # 用户消息、AI 回答、新建会话全部回滚丢失。
    # 在任何 API 调用（分类/检索）之前提交，把断开窗口归零；
    # 计数已在上方统一 +1。
    await db.commit()

    # 主路径（分类→检索→生成→落库→done）由 _stream_answer 承担，
    # 与 regenerate_answer（重答旧问题）共用同一套实现
    async for event in _stream_answer(
        db, user, conversation, message_text, role, start_time,
        exclude_message_id=user_message.id,
        trace_id=trace_id,
        external_history=external_history,
        capture_usage=capture_usage,
        kb_restrict=kb_restrict,
    ):
        yield event


async def _stream_answer(
    db: AsyncSession,
    user: User,
    conversation: Conversation,
    message_text: str,
    role: str,
    start_time: float,
    exclude_message_id: str | None = None,
    trace_id: str | None = None,
    external_history: list[dict] | None = None,
    capture_usage: bool = False,
    kb_restrict: list[str] | None = None,
) -> AsyncGenerator[str, None]:
    """AI 回答主路径：分类 → 检索 → 生成 → 落库 → done。

    stream_chat（新问题）与 regenerate_answer（重答旧问题）共用，
    SSE 事件序列完全一致。exclude_message_id 把"被重答的那条 user 消息"
    排除出多轮历史，防止该问题在 LLM 上下文出现两次。
    external_history（批次A·11e backlog）：外部注入的对话历史
    （[{"role","content"}...]，OpenAI 兼容门 messages 直传）——非 None 时
    替代 DB 会话历史（客户端自带历史，内部会话不累积）。
    kb_restrict（批次F 挂清单 #12）：接入凭证库白名单（API key kb_ids），
    与账号可见范围取交集——跨部门代建受限 key 的落地通道。
    """
    try:
        # 0. 意图识别 + 分类：一次 LLM 调用同时完成
        category_ids = None
        classified_name = None
        clarified_question = message_text  # 默认用原始问题
        flat_cats: list = []
        result = None
        t_stage = time.time()

        # ── 可见范围（企业知识库阶段1）：检索全部限定在用户可见库/文档内 ──
        # 用户消息已在上方 commit；这里的范围计算决定本次问答能碰哪些文档。
        visible_kb_ids = await kb_service.get_visible_kb_ids(db, user)
        doc_ids = await kb_service.get_visible_doc_ids(db, user)
        # 批次F 挂清单 #12：key 库白名单 ∩ 账号可见范围（同 MCP 凭证先例
        # mcp_service.search_for_user——批量查文档库归属，不逐条查询）
        if kb_restrict and doc_ids:
            restrict_kb_ids = set(kb_restrict)
            kb_rows = (await db.execute(
                select(Document.id, Document.kb_id).where(Document.id.in_(doc_ids))
            )).all()
            allowed = {rid for rid, rkb in kb_rows if rkb in restrict_kb_ids}
            doc_ids = [d for d in doc_ids if d in allowed]
        # 批次B：树遍历腿的树结构缓存（dirty 才重查，检索前保证就绪）
        from app.rag import raptor as raptor_mod

        await raptor_mod.refresh_tree_cache(db)
        if not doc_ids:
            # 用户没有任何可读知识库：直接提示，不调 LLM/检索
            no_access_msg = (
                "您当前没有可访问的知识库内容。\n\n"
                "如有疑问，请联系管理员开通相应知识库权限。"
            )
            assistant_message = Message(
                conversation_id=conversation.id,
                role="assistant",
                content=no_access_msg,
                citations=None,
                token_count=len(no_access_msg),
                latency_ms=0,
                created_at=tz_now(),
            )
            db.add(assistant_message)
            await conversation_service.bump_message_count(db, conversation.id, 1)
            await db.flush()
            # 批次F 验收修复：done 前显式 commit（同主路径竞态修复）
            try:
                await db.commit()
            except Exception as e:
                logger.warning("无权限提示落库失败: %s", e)
            yield _sse_event("token", {"type": "text_delta", "content": no_access_msg})
            yield _sse_event("done", {
                "type": "done",
                "conversation_id": conversation.id,
                "message_id": assistant_message.id,
                "latency_ms": 0,
                "token_count": len(no_access_msg),
            })
            return

        # 延迟优化 A+C（自适应检索）：
        # - 小库（可见分块 ≤ FAST_PATH_MAX_CHUNKS）：分类（LLM）与可见库检索（嵌入 API）并行，
        #   一次检索 + 代码后置挑拣（分类内优先、不足补可见库），
        #   消除 L1→L2→L3 串行多轮 API 调用（小库分类内几乎必然凑不满 ANSWER_K）；
        # - 大库：保持逐级下钻（分类过滤检索，防全库噪声淹没冷门分类）。
        # 注意：小库判定基于"用户可见分块数"而非全库总数（防越权全库检索）。
        # 统计失败（向量库故障）时按小库处理：随后检索会抛错并走统一错误事件。
        try:
            visible_chunk_count = await asyncio.to_thread(count_chunks, doc_ids)
            small_corpus = visible_chunk_count <= FAST_PATH_MAX_CHUNKS
        except Exception as e:
            logger.warning(f"获取可见分块统计失败，按小库路径继续: {e}")
            small_corpus = True

        try:
            categories = await document_service.get_categories(db, kb_ids=visible_kb_ids)
            flat_cats = _flatten_for_classification(categories) if categories else []
        except Exception as e:
            logger.warning(f"获取分类树失败，退化为无分类检索: {e}")
            categories = []
            flat_cats = []

        # 多轮对话历史（提前获取：既用于检索词扩展，也用于稍后的 LLM 上下文）
        # 批次A·11e backlog：外部历史（OpenAI 门）优先，否则走内部会话历史
        chat_history = (
            external_history
            if external_history is not None
            else await _get_chat_history(
                db, conversation.id, exclude_message_id=exclude_message_id
            )
        )

        async def _safe_classify() -> dict | None:
            """分类调用（含降级）：失败/无分类树/超时返回 None，回退可见库检索。
            批次5 A1：wait_for 超时保护（分类 LLM 挂起不拖垮请求）"""
            if not flat_cats:
                return None
            try:
                # 批次4 可观测：分类阶段 span（属性须在 with 块内设置——
                # span 退出即 end，之后再 set_attribute 无效）
                from app.observability import rag_span

                t_cls = time.time()
                with rag_span("rag.classify") as cls_span:
                    out = await asyncio.wait_for(
                        asyncio.to_thread(classify_question, message_text, flat_cats),
                        settings.llm_timeout_seconds,
                    )
                    if cls_span is not None:
                        cls_span.set_attribute(
                            "classify_ms", int((time.time() - t_cls) * 1000)
                        )
                        if out:
                            cls_span.set_attribute("complexity", out.get("complexity", "simple"))
                            cls_span.set_attribute("category", out.get("category_name", ""))
                return out
            except asyncio.TimeoutError:
                logger.warning("意图+分类超时，回退到可见库检索")
                return None
            except Exception as e:
                logger.warning(f"意图+分类失败，回退到可见库检索: {e}")
                return None

        # 验收批次1·8i：通知/公告类文档时效衰减（时态感知检索）——
        # 可见范围内按"文件名含通知/公告"挑出文档，按发布天数算衰减系数；
        # 提前到快路/编排/大库所有检索路径之前统一计算，各路径行为一致
        notify_decay = await build_notify_decay(db, doc_ids)

        if small_corpus:
            # 小库快路：分类与可见库检索并行（检索先用原始问题；分类结果用于后置挑拣），
            # 消除小库下 L1→L2→L3 串行多轮 API 调用的延迟。
            yield _sse_event("thinking", {
                "type": "status",
                "message": "正在理解问题并检索知识库...",
            })
            result, retrieved_docs = await asyncio.gather(
                _safe_classify(),
                asyncio.to_thread(
                    retrieve_only,
                    _with_history_context(message_text, chat_history),
                    top_k=RECALL_K,
                    doc_ids=doc_ids,
                    notify_decay=notify_decay,
                ),
            )
        else:
            # 大库：分类先行（澄清后问题更利于逐级检索）
            result = await _safe_classify()

        if result:
            clarified_question = result.get("clarified_question", message_text)
            if result.get("category_id"):
                category_ids = _collect_category_ids(categories, result["category_id"])
                classified_name = result["category_name"]
            yield _sse_event("classified", {
                "type": "classified",
                "category_id": result.get("category_id"),
                "category_name": result.get("category_name", ""),
                "clarified_question": clarified_question,
            })
            logger.info(
                f"意图+分类: [{classified_name or '无'}] "
                f"clarified: {clarified_question[:60]}..."
            )
        classify_ms = int((time.time() - t_stage) * 1000)
        t_stage = time.time()

        # ── 复杂问题编排（产品化 B1）：compare/calculate/multi → 拆解 → 多路检索 → 汇总 ──
        # 拆解失败（LLM 异常/解析失败/子问题不足 2 个）一律回落下方普通检索路径，
        # 绝不让编排失败导致整个问答失败。
        complexity = (result or {}).get("complexity", "simple")
        # 批次F 验收修复：分类器对审批问法的 complexity 判定非确定——
        # 同一问法（"报销 1000 元谁审批"）在 approval 与 calculate 间漂移，
        # 被误判为编排时答案由 LLM 汇总（温度 0.1 非确定），边界题两次
        # 答案矛盾；"请假 10 天怎么办理"类问法还会被判 simple（跳过规则
        # 腿、LLM 自由发挥成百科）。规则匹配本身有业务词+数值双重严格
        # 条件（非审批问法天然不命中），故对**所有**非 approval 复杂度
        # 都先试规则：命中即视为审批题、跳过拆解
        # 质检 P3-5 复核 P1：声明必须在 probe 块之前——块内赋值后
        # 再声明会覆盖命中结果（复用退化为死代码）
        probe_hit: dict | None = None
        rule_direct_answer: str | None = None  # 批次F 验收修复三次：规则腿确定性直出文本（跳过 LLM 生成）
        kg_direct_answer: str | None = None  # 批次F 验收修复四次：图谱腿确定性直出（邻居/路径形态）
        if complexity != "approval":
            from app.rag.rules import service as rules_service

            # 质检 P3-5：probe 挂 span（与正式规则匹配同款可观测）
            from app.observability import rag_span

            with rag_span("rag.rules.probe") as probe_span:
                probe_hit = await rules_service.match_question(message_text)
                if probe_span is not None:
                    probe_span.set_attribute("matched", probe_hit is not None)
            if probe_hit:
                complexity = "approval"  # 走规则腿确定性答案（下方规则分支）
        agentic_docs: list | None = None
        agentic_subs: list[str] = []
        agentic_extra: str | None = None
        rule_hit: dict | None = None  # 批次F 验收修复：块外声明（规则分支与生成段共用）
        # 质检 P2-1：approval 不拆解——规则腿确定性回答无需子问题拆解，
        # 拆解的子问题覆盖指令会与规则伪块"仅依据规则回答"冲突（重演
        # 审批题答成百科），且每条规则题白花一次拆解 LLM 调用。
        # 批次F 验收四次修复：relation 同样不拆解——图谱腿确定性回答，
        # 拆解后的汇总 LLM 非确定（真实验收：同一问题一次"钱进"一次
        # "暂无"）
        if complexity not in ("simple", "approval", "relation"):
            yield _sse_event("thinking", {
                "type": "status",
                "message": "检测到复杂问题，正在拆解子问题...",
            })
            # 质检 P3-3：拆解走 LLM 专用池（同步 LLM 调用挂共享池时
            # 同样会拖垮检索——与 invoke_with_timeout 同池隔离）；
            # 质检 P3-3 复核：同款 wait_for 超时——请求侧 120s 放弃
            # 等待回落 None（拆解失败回落普通检索是既有设计）；挂起
            # 线程残留只占专用池（与 invoke_with_timeout 同口径）
            from app.rag.llm import run_in_llm_executor

            try:
                subs = await asyncio.wait_for(
                    run_in_llm_executor(decompose_question, clarified_question),
                    settings.llm_timeout_seconds,
                )
            except asyncio.TimeoutError:
                subs = None
            if subs:
                agentic_subs = subs
                collected: list[list] = []
                for i, sub in enumerate(subs):
                    yield _sse_event("thinking", {
                        "type": "status",
                        "message": f"正在检索子问题 {i + 1}/{len(subs)}：{sub[:40]}",
                    })
                    sub_docs = await asyncio.to_thread(
                        retrieve_only,
                        _with_history_context(sub, chat_history),
                        top_k=RECALL_K,
                        doc_ids=doc_ids,
                        notify_decay=notify_decay,
                    )
                    collected.append(sub_docs)
                agentic_docs = merge_documents(collected)[:MAX_CONTEXT_DOCS]
                if not agentic_docs:
                    # 拆解成功但所有子问题检索落空：编排无效，回落普通检索
                    # （不能把快路已检到的文档也覆盖丢弃）
                    agentic_docs = None
                if agentic_docs and complexity == "calculate":
                    yield _sse_event("thinking", {
                        "type": "status",
                        "message": "正在提取数值并精确计算...",
                    })
                    # 挂清单收敛：数值提取走 LLM 专用池（原挂共享池——
                    # 上游挂起会拖垮检索嵌入，与 invoke_with_timeout 同池隔离）
                    from app.rag.llm import run_in_llm_executor

                    try:
                        calc = await asyncio.wait_for(
                            run_in_llm_executor(
                                extract_calculation,
                                message_text,
                                "\n\n".join(d.page_content for d in agentic_docs[:6]),
                            ),
                            settings.llm_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        calc = None
                    if calc:
                        agentic_extra = format_calculation_note(calc)
                yield _sse_event("thinking", {
                    "type": "status",
                    "message": "正在汇总各子问题答案...",
                })
            logger.info(
                f"复杂问题编排: complexity={complexity}, "
                f"拆解={'成功(' + str(len(agentic_subs)) + '个子问题)' if agentic_subs else '失败，回落普通检索'}"
            )

        # ── 危险操作拒绝（批次F 挂清单 #15）：按问题文本识别明确危险意图，
        # 直接拒绝——不执行工具、不走 web 兜底（小库快路已完成的全库
        # 候选检索结果随拒绝一并废弃）。此前此类问题漏到 web 兜底，
        # LLM 反而教学如何读文件（真实验收 6.9）──
        if detect_dangerous_request(message_text):
            refuse_msg = (
                "抱歉，该操作超出安全范围，已拒绝。\n\n"
                "本系统仅支持知识库检索与内置计算工具（计算/日期），"
                "不支持读取本机文件、删除数据、执行系统命令等外部操作。"
            )
            assistant_message = Message(
                conversation_id=conversation.id,
                role="assistant",
                content=refuse_msg,
                citations=None,
                token_count=len(refuse_msg),
                latency_ms=0,
                created_at=tz_now(),
            )
            db.add(assistant_message)
            await conversation_service.bump_message_count(db, conversation.id, 1)
            await db.flush()
            try:
                await db.commit()
            except Exception as e:
                logger.warning("危险拒绝提示落库失败: %s", e)
            yield _sse_event("token", {"type": "text_delta", "content": refuse_msg})
            yield _sse_event("done", {
                "type": "done",
                "conversation_id": conversation.id,
                "message_id": assistant_message.id,
                "latency_ms": int((time.time() - start_time) * 1000),
                "token_count": len(refuse_msg),
            })
            return

        # ── 工具调用循环（产品化 B3）：启发式闸门 → LLM 决策 → 程序执行 → 结果注入 ──
        # 决策出的工具名必须在候选内（防 LLM 幻觉调用任意工具）；执行失败静默跳过。
        # 外部 MCP Server 配置存在时每次都列工具（一般就 1 个服务，开销可忽略）。
        # 批次F 挂清单 #13：MCP 配置页面化——每问开头从 DB 刷新配置缓存
        await refresh_mcp_config()
        tool_note: str | None = None
        web_hit = False  # web 兜底命中（挂清单 #16：正文 [网络] 补标判据）
        if should_try_tools(message_text):
            # 批次C·11b：沙箱开关关闭时 python 工具不进候选
            # （受限为默认——LLM 决策看不到就不调）
            candidates = [
                {
                    "name": name,
                    "description": spec["description"],
                    # 批次F 验收修复（#13 端到端验证）：参数声明进决策
                    # 提示词——此前内置工具也没带（LLM 靠描述猜参数名）；
                    # 质检 P3：空 schema 不渲染（json.dumps({}) 为真值会
                    # 出"参数：{}"噪音行）
                    "args_schema": json.dumps(spec.get("args_schema"), ensure_ascii=False)
                    if spec.get("args_schema") else "",
                }
                for name, spec in BUILTIN_TOOLS.items()
                if name != "python" or settings.agent_sandbox_enabled
            ]
            candidates.extend(await list_external_tools())
            decision = await detect_tool_call(message_text, candidates)
            if decision:
                yield _sse_event("thinking", {
                    "type": "status",
                    "message": f"正在调用工具：{decision['tool']}...",
                })
                result = await execute_tool_call(decision)
                # 批次F 验收修复（#13 端到端验证）：工具报参数类错误时带
                # 错误反馈重试一次决策——小模型对参数声明遵从不稳定
                # （真实验收：echo 上一轮参数正确、下一轮又 -32602 空参数）
                if result is not None and _tool_result_param_error(result):
                    retry = await detect_tool_call(
                        message_text, candidates,
                        feedback=f"{decision['tool']} 返回：{result[:200]}",
                    )
                    if retry and retry["tool"] == decision["tool"]:
                        yield _sse_event("thinking", {
                            "type": "status",
                            "message": f"工具参数有误，正在重试：{decision['tool']}...",
                        })
                        result = await execute_tool_call(retry)
                        decision = retry
                if result is not None:
                    tool_note = format_tool_note(decision["tool"], result)
                    # 工具调用留痕：外部 MCP 工具与内置工具分开记（审计页可查"AI 调了什么"）
                    await audit_service.record_audit(
                        db, user.id, user.username,
                        "mcp.tool_call" if "." in decision["tool"] else "tool.call",
                        resource_type="tool",
                        detail={
                            "tool": decision["tool"],
                            "args": {
                                k: str(v)[:50] for k, v in decision.get("args", {}).items()
                            },
                        },
                    )
                    # 只记工具名与参数键，不记结果文本（外部工具结果可能含敏感业务数据，
                    # 不应进日志；结果仅注入当次回答上下文）
                    logger.info(
                        f"工具调用: {decision['tool']} "
                        f"参数: {list(decision.get('args', {}).keys())}"
                    )

        # 分类ID → 路径映射（用于来源的分类路径展示）
        id_to_path = {c["id"]: c["path"] for c in flat_cats}

        # ── 检索：小库快路（后置挑拣）/ 大库逐级下钻 ──
        # P3-1 语义（不足 ANSWER_K 即向更宽范围补足）在两条路径下均保留：
        # - 快路：全库候选已在手，后置挑拣即"补足"；
        # - 逐级下钻：L1→L2→L3 每级不足即扩一级。
        parent_category_ids = None  # L2 扩搜用（仅大库路径使用）
        search_level = "L1"

        # 验收批次1·1d：库级检索参数（分类命中 → 该分类所属库的检索配置；
        # 无配置回全局默认）。库配置 = {top_k, pagerank_alpha}
        kb_retrieval_cfg: dict = {}
        if category_ids:
            cat_kb_id = next(
                (c.get("kb_id") for c in flat_cats
                 if c.get("id") in category_ids and c.get("kb_id")),
                None,
            )
            if cat_kb_id:
                kb_row = (await db.execute(
                    select(KnowledgeBase).where(KnowledgeBase.id == cat_kb_id)
                )).scalar_one_or_none()
                kb_retrieval_cfg = (kb_row.retrieval_config or {}) if kb_row else {}
        kb_recall_k = int(kb_retrieval_cfg.get("top_k") or RECALL_K)
        kb_pagerank_alpha = kb_retrieval_cfg.get("pagerank_alpha")


        if agentic_docs is not None:
            # 编排路径：多路检索结果已就绪，跳过分类逐级检索（category 收窄对
            # "对比/计算"类问题反而有害——不同主题分属不同分类）
            retrieved_docs = agentic_docs
            search_level = "AGENTIC"
            category_ids = None
            parent_category_ids = None
        elif small_corpus:
            # 快路后置挑拣：分类内文档优先，不足 RECALL_K 用全库其余文档补足
            retrieved_docs, category_hit = _order_by_category_priority(
                retrieved_docs, category_ids, RECALL_K
            )
            # 快路日志口径：L1=分类内命中优先，L3=全库兜底（语义与大路逐级下钻对齐）
            search_level = "L1" if category_hit else "L3"
            if not category_hit:
                category_ids = None  # 与旧 L3 行为一致
        else:
            # 大库：L1 子分类 → L2 父分类 → L3 可见库兜底
            yield _sse_event("thinking", {
                "type": "status",
                "message": f"正在检索{'（' + classified_name + '）' if classified_name else ''}相关知识...",
            })
            # 批次4 可观测：检索阶段 span（命中块数/层级/top_k）
            from app.observability import rag_span

            t_ret = time.time()
            with rag_span("rag.retrieve", level="L1", top_k=kb_recall_k) as ret_span:
                retrieved_docs = await asyncio.to_thread(
                    retrieve_only, _with_history_context(clarified_question, chat_history),
                    top_k=kb_recall_k, doc_ids=doc_ids, category_ids=category_ids,
                    pagerank_alpha=kb_pagerank_alpha,
                    notify_decay=notify_decay,
                )
                if ret_span is not None:
                    ret_span.set_attribute("retrieve_ms", int((time.time() - t_ret) * 1000))
                    ret_span.set_attribute("hit_chunks", len(retrieved_docs))

            # L2: 父分类扩搜（L1 不足 ANSWER_K 条时触发）
            if len(retrieved_docs) < ANSWER_K and category_ids and result and result.get("category_id"):
                parent_id = _find_parent_id(categories, result["category_id"])
                if parent_id and parent_id != result["category_id"]:
                    parent_category_ids = _collect_category_ids(categories, parent_id)
                    parent_name = _find_category_name(categories, parent_id)
                    search_level = "L2"
                    yield _sse_event("thinking", {
                        "type": "status",
                        "message": f"子分类下资料不足，正在扩展至「{parent_name}」下全部文档...",
                    })
                    retrieved_docs = await asyncio.to_thread(
                        retrieve_only, _with_history_context(clarified_question, chat_history),
                        top_k=kb_recall_k, doc_ids=doc_ids,
                        parent_category_ids=parent_category_ids,
                        pagerank_alpha=kb_pagerank_alpha,
                        notify_decay=notify_decay,
                    )

            # L3 扩搜判定：①条数不足（既有逻辑）②分类过滤结果 rerank 后
            # top1 相关性不足——批次F 验收修复：分类过滤把更高相关的跨
            # 分类文档排除在外（"会议室预订"路由到"会议室使用"分类，
            # 挂在"会议室"分类下的测试文档进不来，答案答成另一份文档；
            # L1 top1=0.628 < 阈值 0.7 而全库 top1=0.785）。
            # 批次F 验收修复二次：候选 6~10 条的盲区——此前预 rerank 门槛
            # 是 >10 条（复用 RERANK_MIN_CANDIDATES 优化），6~10 条时
            # 条数判定"充足"、rerank 判定"候选少跳过"、预 rerank 也不
            # 执行，分类路由错误完全无法纠正（真实验收："公务用车油费"
            # 路由到"采购"、"团体来访"路由到"劳动合同续签"，均 L1 命中
            # 6 条无扩搜无 rerank，答案误判"暂无"）。门槛降到 ≥ANSWER_K：
            # 分类过滤候选一旦够数就先判分再决定扩搜（6~10 条时多花
            # 一次 rerank 调用，成本低，堵住盲区）
            expand_l3 = len(retrieved_docs) < ANSWER_K
            if (not expand_l3 and (category_ids or parent_category_ids)
                    and len(retrieved_docs) >= ANSWER_K):
                _scored = await asyncio.to_thread(
                    rerank_documents, clarified_question, retrieved_docs,
                    ANSWER_K, notify_decay,
                )
                _top1 = _scored[0].metadata.get("rerank_score") if _scored else None
                if _top1 is not None and _top1 < settings.l1_expand_min_score:
                    expand_l3 = True
                else:
                    # 相关性达标：沿用预 rerank 结果（统一 rerank 块
                    # 对 ≤10 条候选走截断分支，不会重复调用 API）
                    retrieved_docs = _scored

            # L3: 可见库兜底（条数不足或分类内相关性不足时触发；
            # 无分类文档在此层级进入候选）
            if expand_l3 and (category_ids or parent_category_ids):
                search_level = "L3"
                yield _sse_event("thinking", {
                    "type": "status",
                    "message": "分类下资料不足，正在全部可见知识库中补充检索...",
                })
                retrieved_docs = await asyncio.to_thread(
                    retrieve_only, _with_history_context(clarified_question, chat_history),
                    top_k=RECALL_K, doc_ids=doc_ids,
                    notify_decay=notify_decay,
                )
                category_ids = None
                parent_category_ids = None

        # P3-3 rerank 精排（B 优化：候选 ≤ RERANK_MIN_CANDIDATES 时跳过——
        # 小候选重排收益趋近于零，省一次 API 调用；跳过时直接截断到 ANSWER_K）。
        # 批次语料增强：通知时效衰减系数进 rerank（纯语义排序会把内容几乎
        # 相同的跨年度通知排乱，衰减必须在精排层介入）
        if len(retrieved_docs) > RERANK_MIN_CANDIDATES:
            retrieved_docs = await asyncio.to_thread(
                rerank_documents, clarified_question, retrieved_docs, ANSWER_K,
                notify_decay,
            )
        else:
            retrieved_docs = retrieved_docs[:ANSWER_K]

        # 批次F 挂清单 #14：内置工具命中且检索弱相关时清空检索来源——
        # 工具结果是程序算出的确定性事实，弱相关文档混入会让来源标注
        # 不清（真实验收："12345 加 6789"来源出现广告投放登记表.csv）。
        # 相关性达标（如"迟到扣款计算"命中制度条款）时保留检索上下文。
        # 质检 P3：小库截断路径无 rerank 分数（_retrieval_irrelevant 对
        # 无分候选一律 False）——工具命中时补一次精排拿相关性，确保
        # 任何路径下弱相关判定都有分数依据。
        # 批次F 验收扩展（#13 端到端验证发现）：外部 MCP 工具同样适用——
        # 真实验收：echo 成功回显后 LLM 仍引用 [来源2][来源5]（CI 流水线
        # 文档）硬解释"工具不在文档里"，无关来源混入。工具结果（内置或
        # 外部）都是程序执行的确定事实，弱相关来源应一律清掉
        if tool_note and retrieved_docs:
            has_scores = any(
                d.metadata.get("rerank_score") is not None for d in retrieved_docs
            )
            if not has_scores:
                retrieved_docs = await asyncio.to_thread(
                    rerank_documents, clarified_question, retrieved_docs,
                    ANSWER_K, notify_decay,
                )
            if _retrieval_irrelevant(retrieved_docs, complexity):
                retrieved_docs = []

        # 无检索结果（批次A·11f）：先试 Web 搜索兜底——命中则构造 [网络]
        # 伪块继续走生成链路（答案标注网络来源）；未命中/未启用才走
        # 现有无结果提示（不调用 LLM）。
        # 批次语料增强二期：无结果之外增加"低相关"判定——检索层无分数
        # 过滤，知识库外问题也会返回满额弱相关结果（E2E 实测发现兜底
        # 从未点火）；top1 rerank 分低于阈值同样走兜底
        # 批次F 验收修复（6.8）：工具执行成功时不走 web 兜底——工具
        # 结果是程序算出的确定性事实（如"12345+6789=19134"），本应
        # 直接回答；此前计算类问题检索无结果触发 web 兜底并把工具
        # 笔记清掉（下方 tool_note=None），LLM 拿着网络杂文答不出
        # 计算题（真实验收："12345 加 6789"答"知识库暂无"）
        if (not retrieved_docs or _retrieval_irrelevant(retrieved_docs, complexity)) \
                and tool_note is None:
            from app.rag import web_search

            web_hits = await web_search.search_web(clarified_question)
            if web_hits:
                # 批次F 前置小批（质检 P3 混源）：答案来源已切换为网络，
                # 清掉 KB 派生的工具/计算笔记——混入会导致来源标注不清
                # （计算笔记出自知识库文档，与 [网络] 引用口径冲突）
                tool_note = None
                agentic_extra = None
                web_text = (
                    "---- 网络搜索结果（[网络] 引用） ----\n"
                    + "\n\n".join(
                        f"[{i + 1}] {h['title']}\n{h['snippet']}"
                        for i, h in enumerate(web_hits)
                    )
                    + "\n\n知识库无相关内容，请基于上述网络搜索摘要回答，"
                      "引用标注 [网络]；如信息不足以回答，请如实说明。"
                )
                web_doc = LangchainDocument(
                    page_content=web_text,
                    # 批次B P3：web 伪块无真实 doc_id——sources 事件的
                    # doc_id 置空字符串（前端引用跳转对空 doc_id 显示纯文本，
                    # 不发死链请求；[网络] 标注本身说明来源为网络搜索）
                    metadata={"filename": "[网络] 搜索结果",
                              "chunk_type": "web",
                              "document_id": "", "chunk_index": 0},
                )
                retrieved_docs = [web_doc]
                web_hit = True  # 挂清单 #16：done 前正文无 [网络] 强制补标
            else:
                no_docs_msg = "抱歉，知识库中暂无相关信息来回答您的问题。\n\n建议：\n- 尝试用不同关键词描述您的问题\n- 联系知识库管理员补充相关文档，或向相关部门确认"
                assistant_message = Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=no_docs_msg,
                    citations=None,
                    token_count=len(no_docs_msg),
                    latency_ms=0,
                    created_at=tz_now(),  # 显式时间：晚于同请求内先保存的用户消息，保证排序在问题下方
                )
                db.add(assistant_message)
                # 用户消息已在主路径开头计入；无结果提示同样原子 +1
                await conversation_service.bump_message_count(db, conversation.id, 1)
                await db.flush()
                # 批次F 验收修复：done 前显式 commit（同主路径竞态修复）
                try:
                    await db.commit()
                except Exception as e:
                    logger.warning("无结果提示落库失败: %s", e)
                yield _sse_event("token", {"type": "text_delta", "content": no_docs_msg})
                yield _sse_event("done", {
                    "type": "done",
                    "conversation_id": conversation.id,
                    "message_id": assistant_message.id,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "token_count": len(no_docs_msg),
                })
                return

        logger.info(
            f"逐级检索: [{search_level}] {classified_name or '全库'}, "
            f"命中 {len(retrieved_docs)} 条"
        )

        # 4. 发送引用来源（含分类路径与原文片段）
        # 企业知识库改造阶段2：员工端同样下发原文片段——内部问答"答案要有出处可查证"
        sources = []
        for i, doc in enumerate(retrieved_docs, 1):
            doc_id = doc.metadata.get("document_id", "")
            filename = doc.metadata.get("filename", "未知来源")
            chunk_index = doc.metadata.get("chunk_index", 0)
            entry = {
                "index": i,
                "doc_id": doc_id,
                "title": filename,
                "chunk_idx": chunk_index,
                "category_path": _category_path_for(doc.metadata, id_to_path),
                "text": doc.page_content[:200] + (
                    "..." if len(doc.page_content) > 200 else ""
                ),
            }
            sources.append(entry)

        yield _sse_event("sources", {
            "type": "sources",
            "documents": sources,
        })

        retrieve_ms = int((time.time() - t_stage) * 1000)
        t_stage = time.time()

        # 5. 流式生成回答（按角色选 Prompt；管理员额外捕获 token 用量）
        # （chat_history 已在检索前获取：既扩展检索词，也作 LLM 上下文）
        yield _sse_event("token", {"type": "text_delta", "content": ""})

        # P2-3 检索合并：链内不再二次检索，上下文直接复用上方 retrieve_only 的结果，
        # 保证「来源事件」与「喂给 LLM 的上下文」是同一次检索的同一批文档
        # 验收批次1·1c TOC 轻量版：条款/标题层级命中块补同章兄弟块（章节级召回）
        if retrieved_docs:
            # 批次5 压测优化：兄弟块总数上限 6→3——压测显示生成阶段是
            # 最大延迟（avg 5.1s），上下文块数直接决定 prompt 长度；
            # 3 块兄弟补充足以"看懂整章"，评估集回归无影响
            retrieved_docs = await asyncio.to_thread(
                expand_with_chapter_siblings, retrieved_docs, 3, 3
            )
        # 批次11a RAPTOR 折叠树展开：命中摘要块时补子树叶子（宏观问题
        # 的答案载体）；同兄弟块模式，只追加不重排
        if retrieved_docs:
            from app.rag import raptor

            retrieved_docs = await raptor.expand_summary_children(db, retrieved_docs)

        # 验收批次2：关系类问题走知识图谱检索（检索第三腿）——
        # 图谱上下文合成伪块置顶，[KG] 引用格式；图谱不可用/实体链接失败
        # 时静默回退纯向量（介入条件在 graph.query.search 内判断）
        if complexity == "relation" and settings.graphrag_enabled:
            from app.rag.graph import query as graph_query

            # 批次4 可观测：图谱检索 span（是否介入）
            from app.observability import rag_span

            with rag_span("rag.graph") as kg_span:
                # 批次F 验收四次修复：图谱检索用原始问句——多跳检测
                # （"的上级"重复次数）依赖原始结构，clarified 改写会
                # 把"郑浩 的上级的上级"压成"郑浩 上级 汇报关系"、
                # 检测失效退回单跳邻居（真实验收：多跳题只答一级）
                kg = await graph_query.search(message_text)
                if kg_span is not None:
                    kg_span.set_attribute("intervened", kg is not None)
            if kg:
                kg_text = graph_query.format_context(kg)
                kg_doc = LangchainDocument(
                    page_content=kg_text,
                    metadata={"filename": "[KG] 知识图谱", "chunk_type": "kg",
                              "document_id": "kg", "chunk_index": 0},
                )
                retrieved_docs = [kg_doc] + retrieved_docs
                # 批次F 验收四次修复：图谱腿确定性直出（对齐规则腿模式）——
                # 邻居/路径形态的答案模板化，跳过 LLM 生成（AGENTIC 汇总
                # LLM 非确定，真实验收同一问题两次答案不同）；社区聚合
                # 形态（成员列表）仍走 LLM（列表天然模板化价值低）
                neighbors = kg.get("neighbors") or []
                chain_steps = (kg.get("path") or {}).get("steps") or []
                if neighbors or chain_steps:
                    ent = (kg.get("entities") or [""])[0]
                    if chain_steps:
                        # path 形态：steps 是文本列表（"郑浩的上级是钱进"），
                        # 直接串联输出（批次F 验收四次修复：此前误读 nodes
                        # 结构致多跳题未走直出、仍依赖非确定的 LLM 汇总）
                        kg_direct_answer = (
                            f"{ent}的汇报链：{'；'.join(str(s) for s in chain_steps)}。"
                        )
                    else:
                        rel_parts = []
                        for n in neighbors:
                            # 方向语义与 format_context 同口径：out=对方是
                            # 本实体的上级（"钱进是郑浩的上级"）；in=本实体
                            # 是对方的上级
                            if n["direction"] == "out":
                                rel_parts.append(
                                    f"{n['name']}是{ent}的{n['relation']}"
                                )
                            else:
                                rel_parts.append(
                                    f"{ent}是{n['name']}的{n['relation']}"
                                )
                        kg_direct_answer = (
                            f"{ent}的关联：{'；'.join(rel_parts)}。"
                        )
                    kg_direct_answer = "[KG]\n" + kg_direct_answer
        # 批次2-4 规则腿（检索第四腿）：审批链类问题走规则引擎确定性匹配——
        # 命中则规则伪块置顶（[规则] 引用），未命中静默回退正常检索。
        # 冲突处理：伪块注明"规则与文档冲突时以文档为准"（延续 [KG] 哲学）
        if complexity == "approval":
            from app.rag.rules import service as rules_service

            # 批次4 可观测：规则匹配 span（是否命中）
            from app.observability import rag_span

            with rag_span("rag.rules") as rule_span:
                # 质检 P3-5：probe 已命中则直接复用（probe 与正式匹配
                # 同库同表同参数，结果一致；免二次全量加载）
                rule_hit = probe_hit or await rules_service.match_question(message_text)
                if rule_span is not None:
                    rule_span.set_attribute("matched", rule_hit is not None)
            if rule_hit:
                # 批次2-5：审批人是图实体（"财务总监"→赵明远）时挂图谱上下文
                # ——规则答案 + 实体汇报链，规则腿与图谱腿的混合呈现
                graph_ctx_text = ""
                if settings.graphrag_enabled:
                    from app.rag.graph import query as graph_query

                    gctx = await graph_query.entity_context_for_text(
                        rule_hit["approver"]
                    )
                    if gctx:
                        parts = []
                        for n in gctx["neighbors"]:
                            label = (
                                f"本实体是对方的{n['relation']}"
                                if n["direction"] == "in"
                                else f"对方是本实体的{n['relation']}"
                            )
                            parts.append(f"{n['name']}（{label}）")
                        graph_ctx_text = (
                            f"\n关联图谱实体（[KG]）：{gctx['entity']}；"
                            + "；".join(parts)
                        )
                # 批次F 验收修复：完整审批链——chain 含叠加动作（审批+
                # 备案两步），除主项外拼"另需 X 备案"（真实验收："请假
                # 10 天"只答备案漏审批环节）
                chain_parts = [rule_hit["rule_text"]]
                for step in (rule_hit.get("chain") or []):
                    if step["approver"] != rule_hit["approver"] \
                            or step["action"] != rule_hit["action"]:
                        chain_parts.append(
                            f"另需{step['approver']}{step['action']}"
                            f"（{step['rule_text']}）"
                        )
                rule_text = (
                    f"---- 审批链规则（[规则] 引用） ----\n"
                    f"{'；'.join(chain_parts)}。{graph_ctx_text}\n"
                    # 批次F 验收修复：审批题答案收紧——一句话给审批人结论+
                    # 一句依据即可；不展开其他制度条款、不提无关话题、
                    # 不以反问结尾（真实验收：答案扯出差旅费规定+OA 提醒
                    # +结尾反问，审批题被答成百科）。
                    # 验收二次修复：金额/天数区间的边界归属已由规则引擎
                    # 按制度裁决确定（如 1000 元属"1000 至 5000 元"档而非
                    # "以内"档）——LLM 生成时用自己的语义理解重新解读边界
                    # 会推翻裁决（真实验收：多轮对话下"报销 1000"被答成
                    # 直属上级），指令明确"裁决既定不得重新解读"
                    "请直接采用上述规则给出的审批人/审批链（金额与天数的"
                    "区间归属已由规则引擎按制度裁决确定，不要重新解读边界、"
                    "不要对照其他文档自行判断归属）；第一句直接给出完整"
                    "审批链（引用标注 [规则]），最多再补一句依据；不要"
                    "列举额外条件、不要比较其他文档、不要展开任何规则外"
                    "的制度内容，不要反问、不要提供无关建议。"
                )
                rule_doc = LangchainDocument(
                    page_content=rule_text,
                    metadata={"filename": "[规则] 审批链规则", "chunk_type": "rule",
                              "document_id": "rule", "chunk_index": 0},
                )
                retrieved_docs = [rule_doc] + retrieved_docs
                # 批次F 验收修复三次：规则腿确定性直出——LLM 在边界题
                # （"报销 1000"）反复用自己的语义理解推翻规则裁决（多轮
                # 对话下尤其顽固，温度 0+prompt 收紧均无效）；审批答案
                # 本就是模板化文本，由后端拼接直出（跳过 LLM 生成，
                # 省一次生成调用与延迟）。[规则] 行首标注由下方后处理
                # 补标逻辑统一处理
                # 直出拼装（质检 P2 修复）：审批动作在前、备案/报备类以
                # "另需"后缀全量输出——主项为审批类时低档备案步骤曾被
                # 静默丢弃（与叠加语义矛盾）；先列审批类步骤，再列主项
                # （若为备案类加"另需"），最后补其余非审批步骤。二次修复
                # 与拼装实现见 _format_rule_direct_answer docstring
                steps = [s for s in (rule_hit.get("chain") or [])
                         if s["approver"] != rule_hit["approver"]
                         or s["action"] != rule_hit["action"]]
                approve = [s for s in steps if s["action"] in
                           _APPROVAL_ACTIONS]
                others = [s for s in steps if s["action"] not in
                          _APPROVAL_ACTIONS]
                rule_direct_answer = _format_rule_direct_answer(
                    rule_hit, approve, others
                )
        # 产品化 B1/B3：注入"工具查询结果 + 程序计算结果 + 子问题覆盖要求"到上下文尾部
        agentic_instruction: str | None = None
        inject_parts: list[str] = []
        if tool_note:
            inject_parts.append(tool_note)
        if agentic_subs:
            if agentic_extra:
                inject_parts.append(agentic_extra)
            inject_parts.append(
                "【回答要求】请综合回答以下子问题，逐一覆盖，并标注引用来源：\n- "
                + "\n- ".join(agentic_subs)
            )
        if inject_parts:
            agentic_instruction = "\n".join(inject_parts)
        rag_chain = build_rag_chain(
            chat_history=chat_history,
            role=role,
            retrieved_docs=retrieved_docs,
            extra_context=agentic_instruction,
            # 批次F 验收修复：规则腿命中时温度 0——答案既定，
            # 采样随机性会让 [规则] 标注时有时无（真实验收事故）
            temperature=0 if rule_hit else None,
            # 批次F 验收修复（#13 端到端验证）：工具结果在手时检索来源
            # 已清空，空文档提示切换为"直接基于工具结果回答"——此前
            # 提示打架，LLM 写"参考文档明确说明知识库中暂无"的废话
            tool_mode=bool(tool_note),
        )
        # 批次A·11e backlog：capture_usage=True（OpenAI 兼容门）时普通角色
        # 也捕获真实 token 用量（done 事件带 token_usage，门侧转 OpenAI usage 字段）
        usage_handler = TokenUsageHandler() if (role == "admin" or capture_usage) else None
        stream_cfg = {"callbacks": [usage_handler]} if usage_handler else None

        full_answer = ""
        truncated = False
        # 产品化 B1：编排类回答需要更多篇幅（对比/汇总），字数上限放宽一倍
        answer_max = USER_ANSWER_MAX_CHARS * 2 if agentic_subs else USER_ANSWER_MAX_CHARS
        # 批次4 可观测：生成阶段 span（跨整个流式循环保持 open；
        # 上下文块数/角色作为属性）
        from app.observability import rag_span

        async def _generate_buffered(chain) -> str:
            """单次生成并缓冲全文（反思/重试路径需要先看全文再决定下发）"""
            text = ""
            stream = chain.astream(message_text, config=stream_cfg)
            try:
                async for chunk in stream:
                    text += chunk
                    if len(text) > answer_max + USER_ANSWER_MAX_OVER + USER_TAIL_WINDOW:
                        break  # 软截断兜底
            finally:
                await stream.aclose()  # 中断时必须关闭流，释放 DashScope 连接
            return text

        # 批次C·11b 反思循环（可配置开关）：编排类问题（拆解/对比/汇总）
        # 生成后自检，不合格带修正意见重生成（次数上限
        # settings.agent_reflection_max_retries，默认 1；每轮重试后
        # 重新自检，通过即停——批次C.5 从写死 1 次改为可配置循环）；
        # 编排类本身要等全部子检索完成才生成，缓冲下发不伤首字延迟。
        # 普通单问保持原流式路径（首字延迟是压测核心指标）。
        # 批次C.5 P3-12：反思调用的 token 用量累加（并入 done 统计口径）
        reflection_usage_total = {"prompt_tokens": 0,
                                  "completion_tokens": 0,
                                  "total_tokens": 0}
        if rule_direct_answer:
            # 批次F 验收修复三次：规则腿确定性直出——跳过 LLM 生成
            # （LLM 在边界题反复用语义理解推翻规则裁决，多轮对话下
            # 尤其顽固，温度 0+prompt 收紧均无效）；审批答案模板化，
            # 直出零生成调用零 LLM 延迟。行首 [规则] 标注由下方后处理
            # 补标逻辑统一处理
            full_answer = rule_direct_answer
            yield _sse_event("token", {
                "type": "text_delta",
                "content": full_answer,
            })
        elif kg_direct_answer:
            # 批次F 验收修复四次：图谱腿确定性直出——邻居/路径形态
            # 模板化（AGENTIC 汇总 LLM 非确定，真实验收同一问题两次
            # 答案不同）；文本已含 [KG] 行首标注
            full_answer = kg_direct_answer
            yield _sse_event("token", {
                "type": "text_delta",
                "content": full_answer,
            })
        elif agentic_subs and settings.agent_reflection_enabled and role == "user":
            from app.rag import agentic as agentic_mod

            with rag_span("rag.generate", role=role,
                          context_chunks=len(retrieved_docs)) as gen_span:
                full_answer = await _generate_buffered(rag_chain)
                if gen_span is not None:
                    gen_span.set_attribute("answer_chars", len(full_answer))
            # 反思-重试循环：最多 max_retries 次；自检失败（None）静默终止。
            # 批次C.5 质检 P2 修复：历史意见累积列表——每轮 feedback 追加，
            # 重生成时全部注入（防同一缺陷在后续轮反复出现；此前只注
            # 当轮意见，第 2 轮会丢失第 1 轮意见）
            feedback_history: list[str] = []
            for attempt in range(settings.agent_reflection_max_retries):
                # 批次C.5 P3-10：缓冲期心跳（前端等待期间界面不沉默）
                yield _sse_event("thinking", {
                    "type": "status",
                    "message": "正在自检回答质量...",
                })
                reflection = await agentic_mod.reflect_answer(
                    message_text, full_answer,
                    [s for s in agentic_subs if isinstance(s, str)],
                )
                if reflection is None or reflection["pass"]:
                    break  # 通过/自检失败：不再重试
                ru = reflection.get("usage")
                if isinstance(ru, dict):
                    for k in reflection_usage_total:
                        try:
                            reflection_usage_total[k] += int(ru.get(k) or 0)
                        except (TypeError, ValueError):
                            pass
                feedback_history.append(reflection["feedback"])
                # 带修正意见重建上下文链并重生成（temperature 不变，
                # 修正意见以 extra_context 注入——不改变提示词结构）
                fix_context = (agentic_instruction or "") + (
                    "\n【历史自检意见】\n- "
                    + "\n- ".join(feedback_history)
                    + "\n请修正上述全部问题后重新回答。"
                )
                yield _sse_event("thinking", {
                    "type": "status",
                    "message": f"根据自检意见修正回答中（第 {attempt + 1} 次）...",
                })
                retry_chain = build_rag_chain(
                    chat_history=chat_history, role=role,
                    retrieved_docs=retrieved_docs, extra_context=fix_context,
                    # 质检 P3-1：反思重试同样透传温度（规则腿命中 0，
                    # 重试回落 0.1 会让确定性修复在修正轮失效）
                    temperature=0 if rule_hit else None,
                    tool_mode=bool(tool_note),
                )
                with rag_span("rag.generate.retry", role=role) as retry_span:
                    retry_answer = await _generate_buffered(retry_chain)
                    if retry_span is not None:
                        retry_span.set_attribute("answer_chars", len(retry_answer))
                if retry_answer.strip():
                    full_answer = retry_answer
            # 反思路径：缓冲后一次下发最终版
            yield _sse_event("token", {
                "type": "text_delta",
                "content": full_answer,
            })
        else:
            stream = rag_chain.astream(message_text, config=stream_cfg)
            try:
                with rag_span("rag.generate", role=role,
                              context_chunks=len(retrieved_docs)) as gen_span:
                    async for chunk in stream:
                        full_answer += chunk
                        if role == "admin":
                            yield _sse_event("token", {
                                "type": "text_delta",
                                "content": chunk,
                            })
                        elif len(full_answer) <= answer_max + USER_ANSWER_MAX_OVER:
                            # 上限内实时推送（与补句截断上限一致）；超出后静默缓冲，稍后由 done 下发权威文本
                            yield _sse_event("token", {
                                "type": "text_delta",
                                "content": chunk,
                            })
                        elif len(full_answer) > answer_max + USER_ANSWER_MAX_OVER + USER_TAIL_WINDOW:
                            break  # 模型持续超限：停止生成，软截断兜底
                    if gen_span is not None:
                        gen_span.set_attribute("answer_chars", len(full_answer))
            finally:
                await stream.aclose()  # 中断时必须关闭流，释放 DashScope 连接
        generate_ms = int((time.time() - t_stage) * 1000)

        # 7. user 后处理：句界软截断（员工端同样保留 [来源N] 引用，不再去标记）
        if role == "user":
            full_answer, truncated = _truncate_answer(
                full_answer, answer_max
            )
            full_answer = _trim_dangling_citation(full_answer)
        # 8. 解析引用（企业知识库改造阶段2：员工端同样保存引用标记）
        citations = _extract_citations(full_answer, sources)
        # 批次F 验收修复（挂清单 #2/#5）：规则 [规则] 行首补标 + 幻觉
        # 引用标记清理——抽出 _apply_answer_post_process 对所有角色
        # 生效（admin 直出无行首标注、正文残留 [来源8][来源9] 点不到
        # 来源），纯函数可单测
        full_answer = _apply_answer_post_process(full_answer, rule_hit, citations, web_hit)

        # 9. 计算耗时
        latency_ms = int((time.time() - start_time) * 1000)

        # 10. 保存 AI 回复
        if usage_handler and usage_handler.usage:
            token_count = usage_handler.usage.get("total_tokens") or len(full_answer)
        else:
            token_count = len(full_answer)  # 粗略估算
        assistant_message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=full_answer,
            citations=citations if citations else None,
            token_count=token_count,
            latency_ms=latency_ms,
            trace_id=trace_id,  # 批次4 持久化（质检 N2）：主路径刷新后 trace 链接可见
            created_at=tz_now(),  # 显式时间：晚于同请求内先保存的用户消息，保证排序在问题下方
        )
        db.add(assistant_message)

        # 更新会话消息计数（用户消息已在主路径开头计入，此处只加 AI 回复 1 条；原子 +1）
        await conversation_service.bump_message_count(db, conversation.id, 1)
        await db.flush()
        # 批次F 验收修复：done 事件发出前显式 commit——此前依赖 get_db
        # 在响应结束后统一 commit，done 先到前端、前端立即 getConversation
        # 重拉时消息尚未持久化 → 界面只剩用户问题（"回复消失"竞态，
        # 真实验收事故）；commit 失败只留日志不阻断回答（前端有
        # 流式内容回退路径）
        try:
            await db.commit()
        except Exception as e:
            logger.warning("AI 回复落库失败（前端将走流式回退）: %s", e)

        # 11. 完成事件（含调试信息；user 角色携带后处理后的权威内容）
        # 批次4 可观测：trace_id 桥接（请求阶段捕获值优先——SSE 生成器
        # 在请求 span 上下文外执行，此处 get_current_trace_id 恒为 None）
        from app.observability import get_current_trace_id

        done_payload = {
            "type": "done",
            "conversation_id": conversation.id,
            "message_id": assistant_message.id,
            "latency_ms": latency_ms,
            "token_count": token_count,
            "role": role,
            "trace_id": trace_id or get_current_trace_id(),
            "stage_times": {
                "classify_ms": classify_ms,
                "retrieve_ms": retrieve_ms,
                "generate_ms": generate_ms,
                "total_ms": latency_ms,
            },
        }
        if role == "user":
            done_payload["content"] = full_answer
            if truncated:
                done_payload["truncated"] = True
        if usage_handler and usage_handler.usage:
            done_payload["token_usage"] = usage_handler.usage
        # 批次C.5 P3-12：反思调用的 token 用量并入统计口径（存在时合并）
        if any(reflection_usage_total.values()):
            base = done_payload.get("token_usage") or {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            }
            merged = dict(base)
            for k in reflection_usage_total:
                try:
                    merged[k] = int(merged.get(k) or 0) + reflection_usage_total[k]
                except (TypeError, ValueError):
                    pass
            done_payload["token_usage"] = merged
        yield _sse_event("done", done_payload)

    except Exception as e:
        # 修复：不再把 str(e) 原样回传客户端（泄露内部错误细节）；
        # 完整异常只记录在服务端日志。用户消息已在上方 commit，不会丢失。
        # 注意：loguru 的 f-string 直插异常消息时，异常文本含花括号
        # （如 ValueError "... {'document_id'} ..."）会被当占位符导致
        # 日志自身 KeyError（质检 N1）——必须用 {} 占位符传参
        logger.error("生成回答时出错: {}", e, exc_info=True)
        yield _sse_event("error", {
            "type": "error",
            "message": "生成回答时出错，请稍后重试；若问题持续，可联系知识库管理员",
        })


async def prepare_regenerate(
    db: AsyncSession,
    user: User,
    conversation_id: str,
) -> tuple[str, str]:
    """重新生成的校验与旧回答删除（在流式开始前调用，错误才能以 JSON 返回）。

    规则：
    - 会话必须属于当前用户（404）
    - 最后一条 user 消息之后必须已有 AI 回答，否则无从"重新生成"（400）

    删除旧回答并同步 message_count，先落库再流式（与 stream_chat 一致）。
    返回 (待重答问题文本, 该 user 消息 id)。
    """
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
            Conversation.status != "deleted",  # 软删除的会话不可重答（与详情口径一致）
        )
        .with_for_update()  # 并发加固：双开页面同时点"重新生成"时串行化，防双删双写
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc())
    )
    msgs = list(result.scalars().all())  # 时间倒序
    last_user = next((m for m in msgs if m.role == "user"), None)
    if not last_user:
        raise HTTPException(status_code=400, detail="暂无可重新生成的问题")
    stale_assistants = [
        m for m in msgs
        if m.role == "assistant" and m.created_at > last_user.created_at
    ]
    if not stale_assistants:
        raise HTTPException(status_code=400, detail="该问题尚未回答完毕")

    for m in stale_assistants:
        await db.delete(m)
    await conversation_service.bump_message_count(
        db, conversation.id, -len(stale_assistants)
    )
    await db.commit()  # 先落库再流式：客户端断开时旧回答已删、计数一致

    return last_user.content, last_user.id


async def regenerate_answer(
    db: AsyncSession,
    user: User,
    conversation_id: str,
    message_text: str,
    exclude_message_id: str,
    role: str = "user",
    trace_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """流式重答（prepare_regenerate 之后调用）：复用 _stream_answer 主路径。
    trace_id（批次4）：请求阶段捕获值，桥接进 done 事件。"""
    start_time = time.time()
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
            Conversation.status != "deleted",  # 并发窗口：prepare 后被软删则不再写入
        )
    )
    # prepare 校验通过后、流式开始前，会话可能被并发删除（极端竞态）：
    # SSE 已开始无法再返回 JSON 错误，改为下发 error 事件优雅收尾
    conversation = result.scalar_one_or_none()
    if not conversation:
        yield _sse_event("error", {
            "type": "error",
            "message": "会话不存在，请刷新后重试",
        })
        return

    async for event in _stream_answer(
        db, user, conversation, message_text, role, start_time,
        exclude_message_id=exclude_message_id,
        trace_id=trace_id,
    ):
        yield event


def _sse_event(event: str, data: dict) -> str:
    """格式化 SSE 事件"""
    import json
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _extract_citations(answer_text: str, sources: list[dict]) -> list[dict]:
    """
    从回答文本中提取引用标记 [来源N] 并匹配来源文档
    """
    import re
    citations = []

    # 查找所有 [来源N] 标记
    matches = re.findall(r'\[来源(\d+)\]', answer_text)
    seen_indices = set()

    for idx_str in matches:
        idx = int(idx_str)
        if idx in seen_indices or idx > len(sources) or idx < 1:
            continue
        seen_indices.add(idx)
        src = sources[idx - 1]
        citations.append({
            "index": idx,
            "doc_id": src["doc_id"],
            "title": src["title"],
            "text": src["text"],
            "category_path": src.get("category_path", "未知分类"),
        })

    return citations


async def _get_chat_history(
    db: AsyncSession, conversation_id: str, exclude_message_id: str | None = None
) -> list[dict]:
    """获取会话的最近对话历史（用于多轮对话上下文）。

    P3-2 修复：排除当前问题消息（exclude_message_id）——旧实现把刚落库的
    当前问题也带进历史，导致它既出现在历史末尾、又作为 question 单独传一遍。
    """
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(11)  # 多取 1 条：排除当前问题后仍保证最多 10 条真实历史
    )
    messages = result.scalars().all()
    # 反转回时间顺序
    messages.reverse()
    # 过滤 system 提示与当前问题；agent 回复映射为 assistant
    # （客服时代的历史数据可能仍有 role=agent 的消息，兼容展示）
    return [
        {"role": "assistant" if msg.role == "agent" else msg.role, "content": msg.content}
        for msg in messages
        if msg.role != "system" and msg.id != exclude_message_id
    ]


async def _get_or_create_conversation(
    db: AsyncSession,
    user_id: str,
    conversation_id: str | None,
    first_message: str,
) -> Conversation:
    """获取或创建会话。

    阶段5B 修复：已软删除（status="deleted"）的会话视为不存在——
    带已删会话 id 发消息会落进一个新会话，而不是把消息写进已删会话。
    """
    if conversation_id:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.status != "deleted",
            )
        )
        conv = result.scalar_one_or_none()
        if conv:
            return conv

    # 创建新会话，标题用首条消息截取
    title = first_message[:30] + ("..." if len(first_message) > 30 else "")
    conv = Conversation(
        user_id=user_id,
        title=title,
    )
    db.add(conv)
    await db.flush()
    await db.refresh(conv)
    return conv


def _flatten_for_classification(categories: list) -> list[dict]:
    """将分类树扁平化为列表，每项含 {id, name, path, kb_id}，path 如 '手机 > iPhone'"""
    result = []

    def walk(nodes: list, ancestors: list[str]):
        for cat in nodes:
            cat_dict = cat.model_dump() if hasattr(cat, "model_dump") else cat
            path_parts = ancestors + [cat_dict["name"]]
            result.append({
                "id": cat_dict["id"],
                "name": cat_dict["name"],
                "path": " > ".join(path_parts),
                "kb_id": cat_dict.get("kb_id"),
            })
            children = cat_dict.get("children", [])
            if children:
                walk(children, path_parts)

    walk(categories, [])
    return result


def _collect_category_ids(categories: list, matched_id: str) -> list[str]:
    """收集匹配分类及其所有子分类的 ID 列表"""
    ids = []

    def walk(nodes: list):
        for cat in nodes:
            cat_dict = cat.model_dump() if hasattr(cat, "model_dump") else cat
            if cat_dict["id"] == matched_id:
                _collect_descendants(cat_dict, ids)
                return True
            children = cat_dict.get("children", [])
            if children and walk(children):
                return True
        return False

    def _collect_descendants(node: dict, result: list):
        result.append(node["id"])
        for child in node.get("children", []):
            _collect_descendants(child, result)

    walk(categories)
    # 如果没找到匹配的 ID（理论上不会），至少包含 matched_id 本身
    if not ids:
        ids.append(matched_id)
    return ids


def _find_parent_id(categories: list, target_id: str) -> str | None:
    """
    在分类树中查找指定节点的 parent_id。
    如果节点本身就是顶级分类，返回自身 ID。
    用于逐级下钻的 L1→L2 扩搜。

    Examples:
        _find_parent_id("男款通用尺码表ID") → "尺码表ID"
        _find_parent_id("尺码表ID") → "尺码表ID" (顶级，返回自身)
    """
    def walk(nodes: list) -> str | None:
        for cat in nodes:
            cat_dict = cat.model_dump() if hasattr(cat, "model_dump") else cat
            if cat_dict["id"] == target_id:
                return cat_dict.get("parent_id") or cat_dict["id"]
            children = cat_dict.get("children", [])
            if children:
                found = walk(children)
                if found is not None:
                    return found
        return None

    return walk(categories)


def _find_category_name(categories: list, target_id: str) -> str:
    """在分类树中查找指定节点的名称"""
    def walk(nodes: list) -> str | None:
        for cat in nodes:
            cat_dict = cat.model_dump() if hasattr(cat, "model_dump") else cat
            if cat_dict["id"] == target_id:
                return cat_dict["name"]
            children = cat_dict.get("children", [])
            if children:
                found = walk(children)
                if found is not None:
                    return found
        return None

    return walk(categories) or "未知"
