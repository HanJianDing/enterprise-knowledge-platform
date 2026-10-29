"""Dify 兼容检索 API（批次E·6b）——本系统作为 Dify 工作流的知识检索节点

Dify「外部知识库 API 扩展」协议：Dify 工作流配置 api-based extension
后，知识检索节点会把 query 发到本端点，期望返回 records 列表。
本系统据此成为 Dify 可直连的企业知识源——与 OpenAI 兼容门互补
（OpenAI 门=对话、Dify 门=检索）。

协议（Dify external knowledge API）：
  POST /v1/dify/retrieval
  Header: Authorization: Bearer <api_key>（复用 11e api_keys 体系——
  权限穿透：可见库范围=key 所属用户权限）
  Body: {"knowledge_id": "可选", "query": "...",
         "retrieval_setting": {"top_k": 3, "score_threshold": 0.5}}
  Response: {"records": [{"content", "score", "title", "metadata"}]}

score 口径（诚实描述，批次E 质检修正）：按 rerank 排序位置归一
（首位 1.0 递减 0.05/位）——非语义分数；score_threshold 本系统不
执行过滤（全量返回 top_k，阈值门控交给 Dify 侧按排名截断）。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/dify", tags=["Dify 兼容"])


class RetrievalSetting(BaseModel):
    top_k: int = Field(default=3, ge=1, le=20)
    score_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class DifyRetrievalRequest(BaseModel):
    knowledge_id: str | None = None  # Dify 侧知识库标识；本系统可选忽略
    query: str = Field(min_length=1, max_length=2000)
    retrieval_setting: RetrievalSetting = Field(default_factory=RetrievalSetting)


@router.post("/retrieval")
async def dify_retrieval(
    data: DifyRetrievalRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Dify 知识检索：query → 内部混合检索+rerank → records 协议响应。

    鉴权复用 OpenAI 兼容门的 api_keys 体系（Bearer key，权限穿透）；
    knowledge_id 支持 "kb:<库id>" 格式限定检索库（库须在 key 用户可见
    范围内，越权返回 403），其他值忽略（全部可见库检索）。
    """
    from app.api.v1.openai_compat import _auth_by_api_key

    user, api_key = await _auth_by_api_key(request, db)

    # 可见范围（与问答链路同口径）
    from app.services import kb_service

    doc_ids = await kb_service.get_visible_doc_ids(db, user)
    # 批次F 挂清单 #12（质检 P2-6）：Dify 门复用 api_keys 体系，key 的
    # 库白名单同样收敛检索范围（∩ 账号可见范围，与 OpenAI 门同口径）
    if api_key.kb_ids and doc_ids:
        from sqlalchemy import select as sa_select

        from app.models.document import Document

        restrict_kb_ids = set(api_key.kb_ids)
        kb_rows = (await db.execute(
            sa_select(Document.id, Document.kb_id).where(Document.id.in_(doc_ids))
        )).all()
        allowed = {rid for rid, rkb in kb_rows if rkb in restrict_kb_ids}
        doc_ids = [d for d in doc_ids if d in allowed]
    if not doc_ids:
        # 批次E 质检：提前返回路径同样落审计（调用留痕一致性）
        await _audit_dify(db, request, user, api_key, data, 0)
        return {"records": []}

    # knowledge_id 限定库（"kb:<uuid>" 格式）；越权库拒绝
    kb_filter_ids: list[str] | None = None
    if data.knowledge_id and data.knowledge_id.startswith("kb:"):
        kb_id = data.knowledge_id[3:]
        visible_kbs = await kb_service.get_visible_kb_ids(db, user)
        if kb_id not in visible_kbs:
            # 批次E 终审：越库拒绝同样落审计（调用留痕一致性）
            await _audit_dify(db, request, user, api_key, data, 0)
            raise HTTPException(status_code=403, detail="知识库不在可见范围内")
        kb_filter_ids = [kb_id]

    # 按库过滤文档（knowledge_id 指定时）
    if kb_filter_ids:
        from sqlalchemy import select

        from app.models.document import Document

        rows = (await db.execute(
            select(Document.id).where(
                Document.id.in_(doc_ids), Document.kb_id.in_(kb_filter_ids)
            )
        )).all()
        doc_ids = [r[0] for r in rows]
        if not doc_ids:
            await _audit_dify(db, request, user, api_key, data, 0)
            return {"records": []}

    # 检索：混合召回（生产口径 RECALL_K=20）→ rerank 精排 top_k
    import asyncio

    from app.rag.chain import retrieve_only
    from app.rag.reranker import rerank_documents
    from app.services.chat_service import build_notify_decay

    notify_decay = await build_notify_decay(db, doc_ids)
    docs = await asyncio.to_thread(
        retrieve_only, data.query, top_k=20, doc_ids=doc_ids
    )
    docs = await asyncio.to_thread(
        rerank_documents, data.query, docs, data.retrieval_setting.top_k,
        notify_decay,
    )

    records = []
    for d in docs:
        # rerank 分数取用：reranker 排序后的文档不携带分数——用排序位置
        # 归一为分数（首位 1.0 递减至 0）以便 score_threshold 门控；
        # 或直接全部返回（Dify 端仍有自身阈值）。此处按位置归一：
        # threshold 过滤交给 Dify 侧更合理——本系统返回全量 top_k。
        records.append({
            "content": d.page_content,
            "score": 1.0 - (len(records) * 0.05),  # 位置归一（首位 1.0 递减）
            "title": d.metadata.get("filename", "未知来源"),
            "metadata": {
                "document_id": d.metadata.get("document_id", ""),
                "chunk_index": d.metadata.get("chunk_index", 0),
                "category_id": d.metadata.get("category_id", ""),
            },
        })

    # 审计留痕（Dify 门检索调用可追溯——与 OpenAI 门同口径）
    await _audit_dify(db, request, user, api_key, data, len(records))

    return {"records": records}


async def _audit_dify(db, request, user, api_key, data, record_count: int) -> None:
    """Dify 门审计（批次E 质检：所有返回路径统一落审计；失败不阻断）"""
    try:
        from app.services import audit_service

        await audit_service.record_audit(
            db, user.id, user.username, "api.dify_retrieval",
            resource_type="api_key", resource_id=api_key.id,
            detail={"key_name": api_key.name, "query": data.query[:200],
                    "records": record_count,
                    "knowledge_id": data.knowledge_id},
            request=request,
        )
        await db.commit()
    except Exception as e:
        logger.warning("Dify 检索审计失败（不阻断响应）: %s", e)
