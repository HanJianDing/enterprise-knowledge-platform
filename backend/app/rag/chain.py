"""LCEL RAG 链组装"""
from langchain_core.runnables import RunnablePassthrough, RunnableLambda
from langchain_core.output_parsers import StrOutputParser
from langchain_core.messages import HumanMessage, AIMessage

from app.rag.llm import get_llm
from app.rag.retriever import get_retriever, get_hybrid_retriever
from app.rag.prompts import get_rag_prompt
from app.config import settings


def _format_docs(docs, tool_mode: bool = False) -> str:
    """将检索文档格式化为带编号的引用文本。

    tool_mode（批次F 验收修复 #13 端到端验证）：工具结果在手时检索
    来源已按弱相关清空——此前空文档提示"告知用户暂无相关信息"与工具
    成功结果打架，LLM 被带偏写"参考文档明确说明知识库中暂无相关
    文档"的废话（真实验收）。工具模式下改为直接基于工具结果回答。
    """
    if not docs:
        if tool_mode:
            return ("（知识库中暂无相关文档；本次回答请直接基于上文工具查询"
                    "结果输出，不要提及知识库、参考文档或依据）")
        return "（知识库中暂无相关文档，请勿使用自己的知识回答业务问题，告知用户暂无相关信息）"
    formatted = []
    for i, doc in enumerate(docs, 1):
        source = doc.metadata.get("filename", "未知来源")
        formatted.append(f"[来源{i}] **{source}**\n{doc.page_content}")
    return "\n\n---\n\n".join(formatted)


def _format_chat_history(messages: list) -> str:
    """将消息列表格式化为对话历史文本"""
    if not messages:
        return "（无历史对话）"
    lines = []
    for msg in messages[-10:]:  # 最多取最近10条
        role = "用户" if msg["role"] == "user" else "客服"
        lines.append(f"{role}: {msg['content']}")
    return "\n".join(lines)


def build_rag_chain(
    chat_history: list | None = None,
    use_hybrid: bool = True,
    category_ids: list[str] | None = None,
    parent_category_ids: list[str] | None = None,
    role: str = "user",
    retrieved_docs: list | None = None,
    doc_ids: list[str] | None = None,
    extra_context: str | None = None,
    temperature: float | None = None,
    tool_mode: bool = False,
):
    """
    使用 LCEL 构建 RAG 链

    Args:
        chat_history: 对话历史消息列表
        use_hybrid: 是否使用混合检索，默认 True
        category_ids: 子分类ID列表（L1精搜）
        parent_category_ids: 父分类ID列表（L2扩搜），与 category_ids 互斥
        role: 回答角色，"user"=普通用户，"admin"=管理员（决定 System Prompt）
        retrieved_docs: 调用方已检索好的文档。传入后链内不再二次检索，
            上下文直接复用这批文档（修复"来源与上下文不是同一次检索"的 P2-3 问题，
            同时省掉一次向量 + BM25 调用）
        doc_ids: 可见文档 ID 列表（链内自行检索时的权限过滤，fail-closed）
        extra_context: 追加在检索上下文之后的编排信息（产品化 B1：程序计算结果、
            子问题覆盖要求等 Agentic RAG 汇总指令）。与 retrieved_docs 配合使用
            （Agentic 路径必然自检索并传入 retrieved_docs）
        tool_mode（批次F #13 端到端验证）：工具结果在手时检索来源已清空，
            空文档提示切换为"直接基于工具结果回答"（见 _format_docs）
    """
    extra_block = f"\n\n{extra_context}" if extra_context else ""
    if retrieved_docs is not None:
        # 复用已检索文档：来源事件与 LLM 上下文严格一致
        context_runnable = RunnableLambda(
            lambda _: _format_docs(retrieved_docs, tool_mode=tool_mode) + extra_block
        )
    elif use_hybrid:
        try:
            retriever = get_hybrid_retriever(
                top_k=6, doc_ids=doc_ids,
                category_ids=category_ids, parent_category_ids=parent_category_ids,
            )
        except Exception:
            retriever = get_retriever(
                top_k=6, doc_ids=doc_ids,
                category_ids=category_ids, parent_category_ids=parent_category_ids,
            )
        context_runnable = retriever | _format_docs
    else:
        retriever = get_retriever(
            top_k=6, doc_ids=doc_ids,
            category_ids=category_ids, parent_category_ids=parent_category_ids,
        )
        context_runnable = retriever | _format_docs

    has_history = bool(chat_history)
    prompt = get_rag_prompt(with_history=has_history, role=role)
    # 批次F 验收修复：规则腿命中场景传 temperature=0——答案是规则既定的，
    # 生成只是组织语言，采样随机性会让引用标注（[规则]）时有时无
    llm = get_llm(streaming=True, temperature=temperature)

    if has_history:
        # 带对话历史的链
        history_text = _format_chat_history(chat_history)
        rag_chain = (
            {
                "context": context_runnable,
                "question": RunnablePassthrough(),
                "chat_history": lambda _: [
                    HumanMessage(content=f"## 对话历史\n{history_text}")
                ],
            }
            | prompt
            | llm
            | StrOutputParser()
        )
    else:
        rag_chain = (
            {
                "context": context_runnable,
                "question": RunnablePassthrough(),
            }
            | prompt
            | llm
            | StrOutputParser()
        )
    return rag_chain


def retrieve_only(
    query: str,
    top_k: int = 6,
    doc_ids: list[str] | None = None,
    category_ids: list[str] | None = None,
    parent_category_ids: list[str] | None = None,
    pagerank_alpha: float | None = None,
    notify_decay: dict[str, float] | None = None,
) -> list:
    """仅检索，不生成回答

    Args:
        query: 检索查询
        top_k: 返回结果数
        doc_ids: 可见文档 ID 列表（必填，权限隔离 fail-closed）
        category_ids: 子分类ID列表（L1精搜）
        parent_category_ids: 父分类ID列表（L2扩搜），与 category_ids 互斥
        pagerank_alpha: 验收批次1·1b pagerank 加成系数；None=全局 settings，
            显式传入用于库级覆盖（1d KB 检索配置）
        notify_decay: 验收批次1·8i {文档id: 时效衰减系数}，通知/公告时态感知
    """
    alpha = settings.retrieval_pagerank_alpha if pagerank_alpha is None else pagerank_alpha
    try:
        retriever = get_hybrid_retriever(
            top_k=top_k, doc_ids=doc_ids,
            category_ids=category_ids, parent_category_ids=parent_category_ids,
            # 验收批次1·1b：pagerank 加成系数（settings/库级配置，0=关闭）
            pagerank_alpha=alpha,
            # 验收批次1·8i：通知/公告时效衰减
            notify_decay=notify_decay,
        )
    except Exception as e:
        # 批次F 验收调试+改进：回退留日志——静默回退掩盖问题
        # （真实验收：hr01 问考勤答"暂无"，根因追踪到分类过滤静默失效）
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "混合检索构建失败，回退无过滤检索: %s", e
        )
        retriever = get_retriever(
            top_k=top_k, doc_ids=doc_ids,
            category_ids=category_ids, parent_category_ids=parent_category_ids,
        )
    docs = retriever.invoke(query)
    docs = docs[:top_k] if len(docs) > top_k else docs
    return _expand_to_parents(docs)


def _expand_to_parents(docs: list) -> list:
    """父子双层（验收批次1·1c）：命中子块时返回父块全文。

    - 检索匹配用的是子块文本（小块向量更聚焦），此处把 page_content
      替换为 metadata.parent_content（父块全文）供 LLM 上下文；
    - 同一父块的多个子块命中时去重（按 parent_id 保留首次命中，即
      排序更靠前的那一个）。
    """
    seen_parents: set = set()
    out: list = []
    for d in docs:
        meta = d.metadata or {}
        parent_content = meta.get("parent_content")
        parent_id = meta.get("parent_id")
        if parent_content and parent_id:
            if parent_id in seen_parents:
                continue
            seen_parents.add(parent_id)
            d = d.copy(update={"page_content": parent_content})
        out.append(d)
    return out
