"""知识库管理 API（企业知识库改造 阶段1：按库权限控制，不再全员 require_admin）

权限口径：
- 上传文档：对目标库有 can_upload（admin 恒有）
- 查看文档/分类/统计：可见库范围（can_read）
- 删除文档：can_manage，或 can_upload 且文档是自己传的
- 创建/删除分类：对所属库有 can_manage
- 清理孤立分块：仅 admin（全库级操作）
"""
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Query, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_db, get_current_user, require_admin
from app.models.document import Document, KnowledgeCategory
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.schemas.document import (
    DocumentResponse, DocumentUploadResponse,
    CategoryCreate, CategoryResponse, KnowledgeStats,
    ChunkResponse, ChunkListResponse,
)
from app.schemas.common import MessageResponse, PaginatedResponse
from app.services import audit_service, document_service, kb_service

router = APIRouter(prefix="/api/v1/knowledge", tags=["知识库管理"])


# ── 文档管理 ──

@router.post("/documents/upload", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    kb_id: str = Form(...),
    category_id: str | None = Form(None),
    content_type: str = Form("long_text"),
    request: Request = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传文档到指定知识库（需该库 can_upload 权限）"""
    # 校验 content_type
    allowed_types = {"faq", "structured", "long_text", "chat_log", "table"}
    if content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的内容类型: {content_type}，允许: {', '.join(allowed_types)}",
        )
    await kb_service.require_kb_perm(db, current_user, kb_id, "can_upload")
    resp = await document_service.upload_document(
        db, current_user, file, kb_id, category_id, content_type
    )
    await audit_service.record_audit(db, current_user.id, current_user.username, "document.upload", resource_type="document", resource_id=resp.id,
        detail={"filename": resp.filename, "kb_id": kb_id}, request=request,
    )
    return resp


@router.get("/documents", response_model=PaginatedResponse)
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    search: str | None = Query(None),
    kb_id: str | None = Query(None),
    category_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """文档列表（当前用户可见知识库范围；kb_id 指定单库；category_id 分类筛选）"""
    visible_kb_ids = await kb_service.get_visible_kb_ids(db, current_user)
    if kb_id:
        if kb_id not in visible_kb_ids:
            raise HTTPException(
                status_code=403, detail="没有该知识库的查看权限"
            )
        scope = [kb_id]
    else:
        scope = visible_kb_ids
    docs, total = await document_service.get_documents(
        db, page, page_size, status, search, kb_ids=scope, category_id=category_id
    )
    return PaginatedResponse(
        items=docs, total=total, page=page, page_size=page_size,
    )


@router.post("/documents/{document_id}/reprocess", response_model=MessageResponse)
async def reprocess_document(
    document_id: str,
    force: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """重试处理失败的文档（阶段6：需该文档所属库 can_upload 权限）。

    force=true 时允许对已完成文档"重新分块"（产品化 A1：分块策略升级后
    存量文档按新模板重切——默认仍仅失败态可重试，守住防白烧嵌入成本的质量修复）。
    """
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    await kb_service.require_kb_perm(db, current_user, doc.kb_id, "can_upload")
    # 只允许失败态重试：completed 重复重试会白烧嵌入成本，处理中重试无意义；
    # force=true 是管理员主动确认的"重切"动作，仅放开 completed 态
    if doc.status == "failed" or (force and doc.status == "completed"):
        await document_service.reprocess_document(db, document_id, force=force)
        # 重试/重切留痕（产品化 C6 backlog LOW 项：此前重试无审计）
        await audit_service.record_audit(db, current_user.id, current_user.username, "document.reprocess",
            resource_type="document", resource_id=document_id,
            detail={"force": force, "filename": doc.original_name},
        )
        return MessageResponse(message="文档已重新进入处理队列")
    if doc.status == "completed":
        raise HTTPException(status_code=400, detail="已完成文档需 force=true 重新分块")
    raise HTTPException(status_code=400, detail="仅失败或已完成的文档可以重新处理")


@router.get("/documents/{document_id}/chunks", response_model=ChunkListResponse)
async def get_document_chunks(
    document_id: str,
    around: int | None = None,
    radius: int = Query(1, ge=0, le=10),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取文档的分块（需对该文档所属库有读权限）。

    around 指定时只返回该块附近（±radius）的切片——聊天页引用"查看上下文"用，
    total 仍为全部分块数，前端据此展示"第 N / M 块"。
    """
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    await kb_service.require_kb_perm(db, current_user, doc.kb_id, "can_read")
    return await document_service.get_document_chunks(db, document_id, around=around, radius=radius)


@router.delete("/documents/{document_id}", response_model=MessageResponse)
async def delete_document(
    document_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除文档（含向量数据）。

    权限：can_manage 任意删；can_upload 只能删自己传的文档。
    """
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    perm = await kb_service.get_kb_perm(db, current_user, doc.kb_id)
    if not perm.get("can_manage") and not (
        perm.get("can_upload") and doc.uploaded_by == current_user.id
    ):
        raise HTTPException(
            status_code=403, detail="没有删除该文档的权限（需库管理权限或文档由本人上传）"
        )
    await document_service.delete_document(db, document_id)
    await audit_service.record_audit(db, current_user.id, current_user.username, "document.delete", resource_type="document", resource_id=document_id,
        detail={"filename": doc.original_name, "kb_id": doc.kb_id}, request=request,
    )
    return MessageResponse(message="文档已删除")


# ── 分类管理 ──

@router.get("/categories", response_model=list[CategoryResponse])
async def list_categories(
    kb_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取分类树（可见库范围；kb_id 指定单库）"""
    visible_kb_ids = await kb_service.get_visible_kb_ids(db, current_user)
    if kb_id:
        if kb_id not in visible_kb_ids:
            raise HTTPException(
                status_code=403, detail="没有该知识库的查看权限"
            )
        scope = [kb_id]
    else:
        scope = visible_kb_ids
    return await document_service.get_categories(db, kb_ids=scope)


@router.post("/categories", response_model=CategoryResponse, status_code=201)
async def create_category(
    data: CategoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建分类（分类归属知识库；需该库 can_manage 权限）"""
    kb_id = data.kb_id
    if not kb_id:
        raise HTTPException(status_code=400, detail="必须指定分类所属的知识库 kb_id")
    await kb_service.require_kb_perm(db, current_user, kb_id, "can_manage")
    cat = await document_service.create_category(db, data, kb_id)
    await audit_service.record_audit(db, current_user.id, current_user.username, "category.create", resource_type="category", resource_id=cat.id,
        detail={"name": cat.name, "kb_id": kb_id},
    )
    return cat


@router.delete("/categories/{category_id}", response_model=MessageResponse)
async def delete_category(
    category_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除分类（需所属库 can_manage 权限）"""
    result = await db.execute(
        select(KnowledgeCategory).where(KnowledgeCategory.id == category_id)
    )
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="分类不存在")
    await kb_service.require_kb_perm(db, current_user, cat.kb_id, "can_manage")
    await document_service.delete_category(db, category_id)
    await audit_service.record_audit(db, current_user.id, current_user.username, "category.delete", resource_type="category", resource_id=category_id,
        detail={"name": cat.name, "kb_id": cat.kb_id}, request=request,
    )
    return MessageResponse(message="分类已删除")


# ── 统计 ──

@router.get("/stats", response_model=KnowledgeStats)
async def get_stats(
    kb_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """知识库统计（只读；P2-4 修复：不再附带清理副作用）。

    企业知识库阶段1：kb_id 指定单库（需可见）；不传时按可见库范围统计，
    admin 不传为全局统计。
    """
    if kb_id:
        # 质量审查 LOW 修复：库不存在时明确 404（此前返回全 0 误导调用方）
        kb_result = await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        )
        if not kb_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="知识库不存在")
        await kb_service.require_kb_perm(db, current_user, kb_id, "can_read")
        return await document_service.get_stats(db, kb_id=kb_id)
    if current_user.role == "admin":
        return await document_service.get_stats(db)
    visible_kb_ids = await kb_service.get_visible_kb_ids(db, current_user)
    return await document_service.get_stats(db, kb_ids=visible_kb_ids)


@router.post("/cleanup-orphans", response_model=MessageResponse)
async def cleanup_orphans(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """清理向量库中的孤立分块（P2-4：从 GET /stats 拆出的显式管理操作；仅 admin）"""
    count = await document_service.cleanup_orphans(db)
    return MessageResponse(message=f"已清理 {count} 个孤立分块")
