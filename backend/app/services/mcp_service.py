"""MCP Server 工具实现（产品化 B2）：权限穿透检索 + 可见库列表 + 审计留痕

与业务同会话直写审计（复用 audit_service 的约定：只 add 不 flush，
随本会话事务提交——只有真正落库的操作才留痕）。
"""
import asyncio

from app.infrastructure.database import async_session_factory
from app.models.user import User
from app.rag.chain import retrieve_only
from app.services import audit_service, kb_service


async def search_for_user(
    user: User, question: str, top_k: int = 6, kb_restrict: list[str] | None = None
) -> str:
    """按调用者身份检索知识库（权限 fail-closed），返回自带引用的上下文文本。

    返回结构"自带质量"：片段带 [来源N] 编号与文件名 + 引用指令——
    把生成侧兜底（必须引用、禁止编造）下沉到检索侧，客户端模型照答即可。
    kb_restrict（批次D）：接入凭证的库白名单，与账号可见范围取交集。
    """
    top_k = max(1, min(top_k or 6, 10))
    question = (question or "").strip()
    if not question:
        return "【错误】question 不能为空。"
    async with async_session_factory() as db:
        doc_ids = await kb_service.get_visible_doc_ids(db, user)
        if kb_restrict and doc_ids:
            # 批次D：接入凭证库白名单 ∩ 账号可见范围（批量查文档库归属，不逐条查询）
            from sqlalchemy import select as sa_select

            from app.models.document import Document

            restrict_kb_ids = set(kb_restrict)
            kb_rows = (await db.execute(
                sa_select(Document.id, Document.kb_id).where(Document.id.in_(doc_ids))
            )).all()
            allowed = {rid for rid, rkb in kb_rows if rkb in restrict_kb_ids}
            doc_ids = [d for d in doc_ids if d in allowed]
        if not doc_ids:
            await _audit(db, user, "mcp.search_kb", question, hits=0)
            await db.commit()
            return "该账号当前没有任何可见知识库内容，请联系管理员开通相应库权限。"

        # 批次B 质检 P2-2：MCP 门走 retrieve_only（RRF 三腿）——树遍历腿
        # 需要树结构缓存就绪；与 chat/eval 同口径，检索前刷新（代数未变零成本）
        from app.rag import raptor as raptor_mod

        await raptor_mod.refresh_tree_cache(db)
        docs = await asyncio.to_thread(
            retrieve_only, question, top_k=top_k, doc_ids=doc_ids
        )
        if not docs:
            await _audit(db, user, "mcp.search_kb", question, hits=0)
            await db.commit()
            return "知识库中未找到与该问题相关的内容。"

        lines = [
            "以下片段来自企业知识库检索（按相关度排序）：",
            "",
        ]
        for i, doc in enumerate(docs, 1):
            filename = doc.metadata.get("filename", "未知来源")
            lines.append(f"[来源{i}] {filename}")
            lines.append(doc.page_content)
            lines.append("")
        lines.append(
            "请仅依据以上片段回答用户问题，并在答案中引用 [来源N] 编号；"
            "若片段不足以回答，请明确说明信息不足，不要编造。"
        )
        await _audit(db, user, "mcp.search_kb", question, hits=len(docs))
        await db.commit()  # 审计随本会话提交（无请求级事务，必须显式落库）
        return "\n".join(lines)


async def list_visible_kbs_for_user(
    user: User, kb_restrict: list[str] | None = None
) -> str:
    """列出调用者可见的知识库与权限级别（读/传/管）。

    kb_restrict（批次D）：接入凭证库白名单——列表同样收敛到白名单内，
    与检索的"账号可见 ∩ 白名单"契约一致（质检 S1 修复）。
    """
    from sqlalchemy import select

    from app.models.knowledge_base import KnowledgeBase

    async with async_session_factory() as db:
        perms = await kb_service.get_user_kb_perms(db, user)
        if kb_restrict:
            restrict = set(kb_restrict)
            perms = {k: v for k, v in perms.items() if k in restrict}
        kb_result = await db.execute(select(KnowledgeBase))
        id_to_name = {kb.id: kb.name for kb in kb_result.scalars().all()}
        await _audit(db, user, "mcp.list_kbs", "", hits=len(perms))
        if not perms:
            return "该账号当前没有任何可见知识库，请联系管理员开通权限。"
        lines = ["当前账号可访问的知识库：", ""]
        for kb_id, p in perms.items():
            rights = "、".join(
                name for name, key in (
                    ("管理", "can_manage"), ("上传", "can_upload"), ("查看", "can_read")
                ) if p.get(key)
            )
            lines.append(f"- {id_to_name.get(kb_id, kb_id)}（{rights or '无权限'}）")
        await db.commit()  # 审计随本会话提交（无请求级事务，必须显式落库）
        return "\n".join(lines)


async def _audit(db, user: User, action: str, question: str, hits: int) -> None:
    """MCP 工具调用留痕（detail 带问题摘要与命中数）"""
    await audit_service.record_audit(
        db,
        user.id,
        user.username,
        action,
        resource_type="mcp_tool",
        detail={
            "question": question[:100] or None,
            "hits": hits,
        },
    )
