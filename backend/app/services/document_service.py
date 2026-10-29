"""文档管理服务"""
import asyncio
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from fastapi import HTTPException, status, UploadFile

from loguru import logger

from app.config import settings
from app.infrastructure.database import async_session_factory
from app.models.document import Document, KnowledgeCategory
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.pipeline.processor import process_document
from app.rag.retriever import delete_document_from_store, get_store_stats, get_chunks_by_document
from app.schemas.document import (
    DocumentResponse, DocumentUploadResponse, CategoryCreate,
    CategoryResponse, KnowledgeStats, ChunkResponse, ChunkListResponse,
)

# 后台文档处理任务句柄集合（修复：create_task 返回的 Task 若不持引用，
# 事件循环随时可能 GC 掉它，导致任务静默消失、文档卡在 uploaded 状态）
_background_tasks: set[asyncio.Task] = set()

# 批次F 验收修复：规则解析排除的汇编型副本——与 pagerank._EXCLUDED_SOURCES
# 同原则（副本文本含全部制度条款，规则只应从单行制度原文抽取）
_RULE_PARSE_EXCLUDED = {
    "管理制度汇编.txt",
    "员工手册.txt",
    "制度全集逐条解读.txt",
    "产品与技术白皮书.md",
}

# ── 分类自动建立（批次F 验收 3.15）──

# 主题名提取要剥离的常见制度类后缀（长词在前：先剥"管理制度"再轮到"制度"）
_THEME_SUFFIXES = (
    "管理制度", "管理规定", "管理办法", "管理流程", "使用规定", "使用流程",
    "审批流程", "申请流程", "申领流程", "操作流程", "登记表", "价格表",
    "对照表", "汇总表", "说明书", "指南", "手册", "规范", "制度", "流程",
    "规定", "办法", "标准", "说明", "表",
)
# 形态子分类（分类树约定：主题父分类下挂四个形态）
_FORM_CHILDREN = ("制度", "流程", "表单", "FAQ")


def _extract_theme(original_name: str) -> str:
    """从文件名提取主题名：去扩展名、剥"验收测试-"等前缀与制度类后缀。
    "验收测试-会议室管理制度.txt" → "会议室"；剥无可剥返回空串（保持未分类）。"""
    stem = Path(original_name).stem
    # 前缀剥离（测试/语料生成用的命名前缀，剥掉后主题更贴近真实文档名）
    for prefix in ("验收测试-", "测试-"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    theme = stem
    for suffix in _THEME_SUFFIXES:
        if theme.endswith(suffix) and len(theme) > len(suffix):
            theme = theme[: -len(suffix)]
            break
    return theme.strip("-_ ")


def _form_child_for(original_name: str, content_type: str) -> str:
    """文档形态 → 形态子分类：表格→表单、FAQ→FAQ、文件名含"流程"→流程、其余→制度"""
    if content_type == "table":
        return "表单"
    if content_type == "faq" or "FAQ" in original_name:
        return "FAQ"
    if "流程" in original_name:
        return "流程"
    return "制度"
# 处理中的文档 ID（阶段6：重启恢复与失败重试的去重依据——
# 同一条文档不会被并发处理两次）
_in_flight_doc_ids: set[str] = set()
# 批次5 7a：并发处理限流——批量导入时同时处理任务数上限（settings 可配），
# 保护 LLM/嵌入 API 不被打爆；信号量在任务入口 acquire、处理完成释放
_process_semaphore = asyncio.Semaphore(settings.concurrent_upload_limit)
# 批次5 7c：failed 文档启动自动重试上限（error 事件次数达到即放弃，
# 防每次重启无限重试同一份坏文档；仍可管理端手动重试）
MAX_AUTO_RETRY = 3


async def _db_get_active_version(
    db: AsyncSession, kb_id: str, original_name: str
) -> Document | None:
    """批次5 7b：查同库同名文档的当前 active 版本（completed/processing/uploaded；
    superseded/failed 不算 active——failed 的上传中断不留版本链）"""
    result = await db.execute(
        select(Document)
        .where(
            Document.kb_id == kb_id,
            Document.original_name == original_name,
            Document.status.in_(("completed", "processing", "uploaded")),
        )
        .order_by(Document.version_no.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_version_chain(db: AsyncSession, doc_id: str) -> list[dict]:
    """批次5 7b：版本链（沿 version_of 上溯，含当前文档），旧版在前新版在后。
    返回 [{id, version_no, status, created_at, chunk_count}]，旧版在前"""
    # 双向收集（质检批次5 低#3）：仅上溯从中间旧版打开会漏更新版本——
    # up=沿 version_of 上溯、down=反查 version_of 指向自己的下游；按 id 去重
    # 批次F 前置·diff 视图修复：up/down 必须各自独立 seen 集合——共享
    # seen_ids 会让 down 的起点（=up 的起点）被跳过、down 分支从未生效
    # （从中间旧版打开版本链会漏掉更新版本，质检批次5 低#3 的修复不完整）
    chain: list[Document] = []
    chain_ids: set[str] = set()

    async def _walk(start_id: str, direction: str) -> None:
        seen: set[str] = set()
        current_id = start_id
        while current_id and current_id not in seen:
            seen.add(current_id)
            r = await db.execute(select(Document).where(Document.id == current_id))
            d = r.scalar_one_or_none()
            if d is None:
                break
            if d.id not in chain_ids:
                chain_ids.add(d.id)
                chain.append(d)
            if direction == "up":
                current_id = d.version_of
            else:
                r2 = await db.execute(
                    select(Document).where(Document.version_of == current_id)
                )
                nxt = r2.scalars().first()
                current_id = nxt.id if nxt else None

    await _walk(doc_id, "up")
    await _walk(doc_id, "down")
    chain.sort(key=lambda d: d.version_no)  # 旧版在前
    return [
        {
            "id": d.id,
            "version_no": d.version_no,
            "status": d.status,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "chunk_count": d.chunk_count,
        }
        for d in chain
    ]


async def restore_version(db: AsyncSession, doc_id: str) -> dict:
    """批次5 7b：回溯——把指定版本恢复为 active（completed），
    链上其他版本标 superseded。返回 {restored_id, version_no}。
    目标版本必须是 superseded（active 版本回溯自身无意义）。"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.status not in ("superseded",):
        raise HTTPException(
            status_code=400,
            detail=f"仅被替换的旧版本可回溯（当前状态 {doc.status}）",
        )
    # 链双向收集：先沿 version_of 上溯到链头，再沿 version_of 反查下游——
    # 版本链是单向的（version_of 指向上一个版本），从旧版回溯时
    # 必须反查才能找到更新的版本（只上溯会漏掉当前 active 版）。
    # 批次F 前置·diff 视图修复：up/down 独立 seen——共享 seen_ids 会让
    # down 起点（=up 起点）被跳过、down 从未生效（回溯旧版时当前版
    # 不会被标 superseded，批次5 存量缺陷，diff 测试炸出）
    chain_docs: list[Document] = []
    chain_ids: set[str] = set()

    async def _walk(start_id: str, direction: str) -> None:
        seen: set[str] = set()
        current_id = start_id
        while current_id and current_id not in seen:
            seen.add(current_id)
            r = await db.execute(select(Document).where(Document.id == current_id))
            d = r.scalar_one_or_none()
            if d is None:
                break
            if d.id not in chain_ids:
                chain_ids.add(d.id)
                chain_docs.append(d)
            if direction == "up":
                current_id = d.version_of
            else:  # down：查 version_of 指向当前文档的下一个版本
                r2 = await db.execute(
                    select(Document).where(Document.version_of == current_id)
                )
                nxt = r2.scalars().first()
                current_id = nxt.id if nxt else None

    await _walk(doc_id, "up")
    await _walk(doc_id, "down")

    for d in chain_docs:
        if d.id == doc_id:
            d.status = "completed"
            _append_process_event(d, "restore", f"已回溯为当前版本（第 {d.version_no} 版）")
        elif d.status == "completed":
            d.status = "superseded"
            _append_process_event(d, "superseded", f"被第 {doc.version_no} 版回溯替换")
    # 质检批次5 低#10：不在此 commit——由端点统一 commit，回溯状态变更与
    # 审计记录同事务（审计失败时回溯一并回滚，证据链完整）
    return {"restored_id": doc_id, "version_no": doc.version_no}


def _new_event(stage: str, detail: str) -> dict:
    """构造一条解析流水事件（产品化 A3：前端时间轴展示用）"""
    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stage": stage,
        "detail": detail,
    }


def _append_process_event(doc: Document, stage: str, detail: str) -> None:
    """向文档解析流水追加事件。

    整体替换列表（而非原地 append）：SQLAlchemy 的 JSON 列变更检测靠
    attribute 赋值触发，原地修改嵌套结构不会被标记为 dirty，落库会丢事件。
    保留最近 20 条：高频重试会无限累积，流水只用于排障不需要全量历史。
    """
    doc.process_log = (list(doc.process_log or []) + [_new_event(stage, detail)])[-20:]


async def upload_document(
    db: AsyncSession,
    user: User,
    file: UploadFile,
    kb_id: str,
    category_id: str | None = None,
    content_type: str = "long_text",
) -> DocumentUploadResponse:
    """上传文档并触发处理管道（企业知识库阶段1：文档必须归属一个知识库）"""
    # 验证知识库存在
    kb_result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
    )
    if not kb_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在",
        )
    # 分类必须属于该知识库（防跨库挂分类）
    if category_id:
        cat_result = await db.execute(
            select(KnowledgeCategory).where(
                KnowledgeCategory.id == category_id,
                KnowledgeCategory.kb_id == kb_id,
            )
        )
        if not cat_result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="分类不存在或不属于该知识库",
            )

    # 验证文件类型（批次D·11d：图片文档 PNG/JPG 进白名单——picture 模板）
    original_name = file.filename or "unknown"
    ext = Path(original_name).suffix.lower()
    allowed_ext = {".pdf", ".txt", ".md", ".docx", ".csv", ".xlsx",
                   ".png", ".jpg", ".jpeg"}
    if ext not in allowed_ext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件格式: {ext}，支持: {', '.join(allowed_ext)}",
        )
    # 批次D·11d：图片扩展名强制 picture 内容类型（无条件覆盖调用方
    # 传入值——图片走 OCR+vision 是唯一正确路径）
    if ext in {".png", ".jpg", ".jpeg"}:
        content_type = "picture"

    # 验证文件大小
    content = await file.read()
    file_size = len(content)
    if file_size > settings.max_upload_size:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"文件过大，最大支持 {settings.max_upload_size // 1024 // 1024}MB",
        )

    # 保存文件到本地（P2-2：阻塞文件 IO 放进线程池，不占事件循环）
    os.makedirs(settings.upload_dir, exist_ok=True)
    doc_id = str(uuid.uuid4())
    filename = f"{doc_id}{ext}"
    storage_path = os.path.join(settings.upload_dir, filename)

    def _write_file() -> None:
        with open(storage_path, "wb") as f:
            f.write(content)

    await asyncio.to_thread(_write_file)

    # 校验文件真实性（魔数检查，含文件读取，同样放线程池）
    from app.pipeline.loader import validate_file
    if not await asyncio.to_thread(validate_file, storage_path):
        await asyncio.to_thread(os.remove, storage_path)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"文件校验失败，文件内容与扩展名不匹配",
        )

    # 批次F 验收修复：加密 PDF 上传时同步拦截（中文弹窗提示）——
    # 此前只靠后台处理管道失败（错误进列表状态），上传本身"成功"、
    # 用户没有即时提示（真实验收 3.9）。pypdf 开卷失败视为损坏，
    # 留给后台管道的 _ERROR_TRANSLATIONS 处理（3.10 语义不变）
    if ext == ".pdf":
        def _check_encrypted() -> bool:
            from pypdf import PdfReader

            reader = PdfReader(storage_path)
            return bool(getattr(reader, "is_encrypted", False))

        try:
            if await asyncio.to_thread(_check_encrypted):
                await asyncio.to_thread(os.remove, storage_path)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="PDF 已加密，请解除密码保护后重新上传",
                )
        except HTTPException:
            raise
        except Exception:
            pass  # 开卷失败（损坏 PDF）：交后台管道出中文提示

    # 批次5 7b 版本管理：同库同名已有文档 → 旧版标 superseded（保留数据与分块，
    # 检索只走 active 版本），新文档 version_of 指向旧版、version_no 递增
    existing = await _db_get_active_version(db, kb_id, original_name)
    if existing is not None:
        existing.status = "superseded"
        _append_process_event(existing, "superseded", "被新版本替换（旧版数据保留，可在版本历史中查看/回溯）")
        version_of = existing.id
        # 质检批次5 低#4：取同库同名全局 max+1（failed 版本不入链但已占号，
        # 按 active 版 +1 会产生平行同号）
        max_v = await db.execute(
            select(func.max(Document.version_no)).where(
                Document.kb_id == kb_id,
                Document.original_name == original_name,
            )
        )
        version_no = (max_v.scalar() or 1) + 1
    else:
        version_of = None
        version_no = 1

    # 批次F 验收修复（3.15）：未选分类时按文件名自动建立主题分类——
    # 主题父分类 + 制度/流程/表单/FAQ 形态子分类（分类树既定形态），
    # 文档挂形态子分类；重传同名沿用旧版分类（版本链内分类一致）。
    # 主题提取失败（剥无可剥）保持未分类，不硬造。
    auto_category_note: str | None = None
    if category_id is None:
        if existing is not None and existing.category_id:
            category_id = existing.category_id  # 版本链沿用旧版分类
        else:
            theme = _extract_theme(original_name)
            if theme:
                # 已有同名主题父分类 → 复用；否则创建父分类 + 四形态子分类
                parent = (await db.execute(
                    select(KnowledgeCategory).where(
                        KnowledgeCategory.kb_id == kb_id,
                        KnowledgeCategory.name == theme,
                        KnowledgeCategory.parent_id.is_(None),
                    )
                )).scalar_one_or_none()
                if parent is None:
                    parent = KnowledgeCategory(
                        name=theme, kb_id=kb_id, parent_id=None
                    )
                    db.add(parent)
                    await db.flush()
                    for form in _FORM_CHILDREN:
                        db.add(KnowledgeCategory(
                            name=form, kb_id=kb_id, parent_id=parent.id
                        ))
                    await db.flush()
                form_name = _form_child_for(original_name, content_type)
                form_cat = (await db.execute(
                    select(KnowledgeCategory).where(
                        KnowledgeCategory.kb_id == kb_id,
                        KnowledgeCategory.parent_id == parent.id,
                        KnowledgeCategory.name == form_name,
                    )
                )).scalar_one_or_none()
                if form_cat is not None:
                    category_id = form_cat.id
                    auto_category_note = f"自动建立分类：{theme} / {form_name}"

    # 创建数据库记录（process_log 首条事件：上传成功、等待处理）
    document = Document(
        id=doc_id,
        filename=filename,
        original_name=original_name,
        file_type=ext.lstrip("."),
        file_size=file_size,
        storage_path=storage_path,
        kb_id=kb_id,
        category_id=category_id,
        content_type=content_type,
        status="uploaded",
        uploaded_by=user.id,
        version_of=version_of,
        version_no=version_no,
        process_log=[_new_event("upload", f"上传成功（{original_name}，第 {version_no} 版，{file_size} 字节）")],
    )
    db.add(document)
    await db.flush()
    if auto_category_note:
        _append_process_event(document, "auto_category", auto_category_note)

    # 推导 parent_category_id（从分类树查询，无需前端传参）
    parent_category_id: str | None = None
    if category_id:
        cat_result = await db.execute(
            select(KnowledgeCategory).where(KnowledgeCategory.id == category_id)
        )
        cat = cat_result.scalar_one_or_none()
        if cat:
            parent_category_id = cat.parent_id if cat.parent_id else category_id

    # 先落库再启动后台任务：任务用自己的 session 查询该记录，
    # 若外层事务还没提交，任务会查不到记录而静默退出（文档永久卡 uploaded）
    await db.commit()

    # 异步处理文档（任务句柄存入模块级集合防 GC，完成时自动移出）
    spawn_processing_task(
        doc_id, storage_path, original_name, content_type, parent_category_id
    )

    return DocumentUploadResponse(
        id=doc_id,
        filename=original_name,
        status="processing",
        message="文档已上传，正在后台处理中...",
    )


def spawn_processing_task(
    doc_id: str,
    file_path: str,
    original_name: str,
    content_type: str = "long_text",
    parent_category_id: str | None = None,
) -> bool:
    """启动后台处理任务（上传/重启恢复/失败重试共用；in-flight 去重）。

    返回 True=已入队，False=已在处理中被跳过。
    """
    if doc_id in _in_flight_doc_ids:
        logger.info(f"文档 {doc_id} 已在处理中，跳过重复入队")
        return False
    _in_flight_doc_ids.add(doc_id)
    task = asyncio.create_task(_limited_process(
        doc_id, file_path, original_name, content_type, parent_category_id
    ))
    _background_tasks.add(task)
    task.add_done_callback(lambda t: (_background_tasks.discard(t), _in_flight_doc_ids.discard(doc_id)))
    return True


async def _limited_process(
    doc_id: str,
    file_path: str,
    original_name: str,
    content_type: str,
    parent_category_id: str | None,
) -> None:
    """批次5 7a：处理入口包并发信号量——超过 concurrent_upload_limit 的任务
    排队等待（信号量 FIFO 语义保证先上传先处理）"""
    async with _process_semaphore:
        await _process_and_update(
            doc_id, file_path, original_name, content_type, parent_category_id
        )


async def recover_processing_documents() -> int:
    """阶段6：启动扫描恢复——进程重启会丢失内存后台任务，
    卡在 uploaded/processing 的文档在启动时重新入队。
    批次5 7c：failed 文档自动重试（error 次数 <3 时重新入队，超限放弃——
    防每次重启无限重试同一份坏文档）。

    返回恢复的文档数量。
    """
    result = await _db_select_stuck_documents()
    recovered = 0
    for doc in result:
        if doc.status == "failed":
            # 7c：自动重试上限（按 process_log 里 error 事件次数计）
            error_count = sum(
                1 for e in (doc.process_log or []) if e.get("stage") == "error"
            )
            if error_count >= MAX_AUTO_RETRY:
                logger.info(
                    f"文档 {doc.original_name} 已重试 {error_count} 次，放弃自动重试（需手动重试）"
                )
                continue
        parent_category_id = None
        if doc.category_id:
            cat = await _db_get_category(doc.category_id)
            if cat:
                parent_category_id = cat.parent_id if cat.parent_id else doc.category_id
        enqueued = spawn_processing_task(
            doc.id, doc.storage_path, doc.original_name,
            doc.content_type, parent_category_id,
        )
        if enqueued:
            recovered += 1
            logger.info(f"启动恢复：重新入队文档 {doc.id}（{doc.original_name}，状态 {doc.status}）")
    if recovered:
        logger.info(f"启动恢复完成：{recovered} 条文档重新进入处理队列")
    return recovered


async def _db_select_stuck_documents() -> list[Document]:
    async with async_session_factory() as db:
        result = await db.execute(
            # 批次5 7c：failed 一并纳入（recover 里按 error 次数过滤重试上限）
            select(Document).where(
                Document.status.in_(("uploaded", "processing", "failed"))
            )
        )
        return list(result.scalars().all())


async def _db_get_category(category_id: str):
    async with async_session_factory() as db:
        result = await db.execute(
            select(KnowledgeCategory).where(KnowledgeCategory.id == category_id)
        )
        return result.scalar_one_or_none()


async def reprocess_document(db: AsyncSession, doc_id: str, force: bool = False) -> None:
    """阶段6：失败/卡住的文档手动重试（权限在路由层校验）。

    force=True（产品化 A1）：已完成文档按当前分块模板"重新分块"——
    处理前先删旧分块（_process_and_update 幂等逻辑），不会翻倍。
    """
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    if not doc.storage_path or not os.path.exists(doc.storage_path):
        raise HTTPException(
            status_code=400, detail="文档物理文件已不存在，无法重试（请重新上传）"
        )
    # 重置状态并落库（与上传路径一致：先落库再入队）
    doc.status = "uploaded"
    doc.error_message = None
    _append_process_event(
        doc, "reprocess",
        "按当前分块模板重新分块" if force else "手动重试，重新进入处理队列",
    )
    await db.commit()

    parent_category_id = None
    if doc.category_id:
        cat_result = await db.execute(
            select(KnowledgeCategory).where(KnowledgeCategory.id == doc.category_id)
        )
        cat = cat_result.scalar_one_or_none()
        if cat:
            parent_category_id = cat.parent_id if cat.parent_id else doc.category_id
    spawn_processing_task(
        doc.id, doc.storage_path, doc.original_name,
        doc.content_type, parent_category_id,
    )
    logger.info(f"手动重试：文档 {doc_id}（{doc.original_name}）已重新入队")


async def _has_newer_version(db: AsyncSession, doc_id: str) -> bool:
    """是否存在指向该文档的更新版本（version_of == doc_id）——
    批次F 验收修复：废止文档的判定依据（比 status 字段可靠，
    重处理重置 status 后信息仍不丢失）"""
    result = await db.execute(
        select(Document.id).where(Document.version_of == doc_id).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _process_and_update(
    doc_id: str,
    file_path: str,
    original_name: str,
    content_type: str = "long_text",
    parent_category_id: str | None = None,
):
    """后台处理文档并更新状态（简化版，使用 asyncio 而非 Celery）。

    修复：不再接收请求级 db 参数（从未被使用，且会拖住请求的连接池连接），
    只使用自己创建的 session。
    """
    from app.infrastructure.database import async_session_factory

    t_start = time.monotonic()
    async with async_session_factory() as session:
        try:
            # 更新为处理中
            result = await session.execute(
                select(Document).where(Document.id == doc_id)
            )
            doc = result.scalar_one_or_none()
            if not doc:
                return
            doc.status = "processing"
            _append_process_event(
                doc, "start", f"开始处理（模式：{content_type}，文件：{original_name}）"
            )
            await session.commit()

            # 阶段6 幂等修复：处理前先清掉该文档可能残留的旧分块——
            # 重试/重启恢复时若上次已写入向量库、状态却未更新，直接重写会分块翻倍
            # （delete_document_from_store 为模块级导入，测试环境可被 monkeypatch 跳过）
            try:
                await asyncio.to_thread(delete_document_from_store, doc_id)
            except Exception as e:
                logger.warning(f"清理旧分块失败（继续处理）: {e}")

            # 执行处理管道（P2-2：加载/分块/向量化全程阻塞，整体放线程池）
            chunk_count, error = await asyncio.to_thread(
                process_document,
                doc_id, file_path, original_name, doc.category_id, content_type,
                parent_category_id=parent_category_id,
            )

            # 更新结果
            doc.chunk_count = chunk_count
            doc.content_type = content_type
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            if error:
                doc.status = "failed"
                doc.error_message = error
                _append_process_event(doc, "error", f"处理失败（耗时 {elapsed_ms}ms）：{error}")
            elif doc.status == "superseded":
                # 质检批次5 中等问题：处理期间被同名重传标 superseded 的文档，
                # 任务完成后不得写回 completed——否则两版同时 active、
                # 旧版分块重新进入检索，破坏"检索只走当前版本"承诺
                _append_process_event(
                    doc, "superseded", f"处理完成但已被新版本替换（{chunk_count} 块数据保留于版本历史）"
                )
            elif await _has_newer_version(session, doc_id):
                # 批次F 验收修复：重处理会重置 status=uploaded，废止文档
                # （有更新版本指向它）处理完成后必须恢复 superseded——
                # 否则废止文档"复活"参与检索，审批题被旧条款误导
                # （真实验收事故：重处理 11 份 2024 版制度后"报销 3000
                # 元"被废止版规则劫持答成财务总监）
                doc.status = "superseded"
                _append_process_event(
                    doc, "superseded", f"重处理完成（{chunk_count} 块数据保留于版本历史）"
                )
            else:
                doc.status = "completed"
                _append_process_event(
                    doc, "done", f"处理完成（耗时 {elapsed_ms}ms，共 {chunk_count} 块）"
                )
            await session.commit()

            # 验收批次2：分块完成后触发图谱建图（fire-and-forget——
            # 图谱是增强腿，失败只留日志，不阻断文档处理主流程）
            if not error and settings.graphrag_enabled:
                from app.rag.graph import graph_service

                # 与上传任务同款约定：任务登记进 _background_tasks（防未处理异常噪音）
                graph_task = asyncio.create_task(
                    graph_service.build_graph_for_document(doc_id, original_name)
                )
                _background_tasks.add(graph_task)
                graph_task.add_done_callback(_background_tasks.discard)
            # 批次2-4 规则腿：分块完成后触发审批规则解析（同样 fire-and-forget；
            # 纯正则解析零 LLM 成本，失败只留日志）。
            # 批次语料增强二期：picture 文档不解析规则——入库文本是
            # OCR/vision 合成描述而非制度原文，OCR 截断残句（"除前述审批"）
            # 会被抽成方向性错误的规则（真实事故：请假审批流程图.png 的
            # 残句压过考勤管理制度现行规则，approval 题 100%→87.5%）
            # 批次F 验收修复：汇编型副本不解析规则——汇编/手册/解读/白皮书
            # 的文本包含全部制度条款（含废止版拼接），副本规则在"最新文档"
            # 裁决里胜出会造成答案漂移（真实验收：汇编重处理触发规则重解析
            # 的中间态，边界题"报销 1000"两次答案不同）；与 pagerank
            # _EXCLUDED_SOURCES 同原则——规则只从单行制度原文抽取
            if not error and content_type != "picture" \
                    and original_name not in _RULE_PARSE_EXCLUDED:
                from app.rag.rules import service as rules_service

                rule_task = asyncio.create_task(
                    rules_service.parse_and_store_for_document(doc_id)
                )
                _background_tasks.add(rule_task)
                rule_task.add_done_callback(_background_tasks.discard)
            # 批次11a RAPTOR 摘要树：厚文档（>raptor_min_chars）分块完成后
            # 自动建树（fire-and-forget，与图谱/规则同款约定）；聚类+LLM
            # 摘要成本只在厚文档上发生，建树失败只留日志、文档正常可用
            if not error:
                from app.rag import raptor

                raptor_task = asyncio.create_task(
                    raptor.build_tree_for_document(doc_id, original_name)
                )
                _background_tasks.add(raptor_task)
                raptor_task.add_done_callback(_background_tasks.discard)

        except Exception as e:
            # 回滚失败事务：若异常发生在 commit 阶段，session 处于 aborted 状态，
            # 不 rollback 直接查询会抛 PendingRollbackError，error 事件将永远写不进
            await session.rollback()
            result = await session.execute(
                select(Document).where(Document.id == doc_id)
            )
            doc = result.scalar_one_or_none()
            if doc:
                doc.status = "failed"
                doc.error_message = str(e)
                _append_process_event(doc, "error", f"处理异常：{e}")
                await session.commit()


async def get_documents(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    status_filter: str | None = None,
    search: str | None = None,
    kb_ids: list[str] | None = None,
    category_id: str | None = None,
) -> tuple[list[DocumentResponse], int]:
    """获取文档列表（kb_ids 为可见库范围；None 表示不限制，仅 admin 调用）

    批次F 验收修复（3.16）：category_id 分类筛选——选主题父分类时包含
    其全部形态子分类的文档（筛选语义与分类树展示一致）。"""
    query = select(Document)

    if kb_ids is not None:
        query = query.where(Document.kb_id.in_(kb_ids))
    if status_filter:
        query = query.where(Document.status == status_filter)
    if search:
        query = query.where(Document.original_name.contains(search))
    if category_id:
        # 收集该分类 + 全部子孙分类（当前树两层：主题→形态；防未来加深）
        cat_scope: list[str] = [category_id]
        frontier = [category_id]
        while frontier:
            kids = (await db.execute(
                select(KnowledgeCategory.id).where(
                    KnowledgeCategory.parent_id.in_(frontier)
                )
            )).scalars().all()
            cat_scope.extend(kids)
            frontier = list(kids)
        query = query.where(Document.category_id.in_(cat_scope))

    # 总数
    count_query = select(func.count(Document.id))
    if kb_ids is not None:
        count_query = count_query.where(Document.kb_id.in_(kb_ids))
    if status_filter:
        count_query = count_query.where(Document.status == status_filter)
    if search:
        count_query = count_query.where(Document.original_name.contains(search))
    if category_id:
        count_query = count_query.where(Document.category_id.in_(cat_scope))
    result = await db.execute(count_query)
    total = result.scalar() or 0

    # 分页（eager load category/kb 关系，避免 N+1）
    query = query.order_by(desc(Document.created_at))
    query = query.options(
        selectinload(Document.category).selectinload(KnowledgeCategory.parent),
        selectinload(Document.kb),
    )
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    documents = result.scalars().all()

    # 构建响应
    docs = []
    for doc in documents:
        d = DocumentResponse.model_validate(doc)
        # 从 eager loaded 关系取分类名/库名（无额外查询）。
        # 批次F 验收：分类名拼完整路径（父分类-子分类）——只看子分类名
        # 满屏"制度/流程"分不清属哪个主题；挂父分类本身的文档只显示父名
        if doc.category:
            parent = doc.category.parent
            d.category_name = (
                f"{parent.name}-{doc.category.name}" if parent else doc.category.name
            )
        if doc.kb:
            d.kb_name = doc.kb.name
        docs.append(d)

    return docs, total


async def delete_document(db: AsyncSession, doc_id: str) -> None:
    """删除文档及其向量数据"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()

    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在",
        )

    # 删除向量数据（ChromaDB，P2-2：阻塞调用放线程池）
    deleted_chunks = 0
    try:
        deleted_chunks = await asyncio.to_thread(delete_document_from_store, doc_id)
        if deleted_chunks == 0:
            logger.warning(f"文档 {doc_id} 在向量库中无分块（可能已被清理）")
    except RuntimeError as e:
        # delete_document_from_store 明确报告删除失败
        logger.error(f"删除向量数据失败 (doc_id={doc_id}): {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"向量数据删除失败，请联系管理员检查 ChromaDB: {str(e)}",
        )
    except Exception as e:
        logger.error(f"删除向量数据异常 (doc_id={doc_id}): {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除向量数据时发生未知错误: {str(e)}",
        )

    # 删除物理文件（阻塞 IO 放线程池）
    def _remove_file() -> None:
        if os.path.exists(doc.storage_path):
            os.remove(doc.storage_path)

    await asyncio.to_thread(_remove_file)

    # 删除数据库记录
    await db.delete(doc)
    await db.commit()

    # 验收批次2：同步清理图谱子图（增量更新——文档删了图不留孤节点；失败只留日志）
    if settings.graphrag_enabled:
        from app.rag.graph import graph_service

        try:
            removed = await graph_service.delete_graph_for_document(doc_id)
            logger.info(f"文档 {doc_id} 图谱子图清理完成（删除 {removed} 条关系）")
        except Exception as e:
            logger.warning(f"文档 {doc_id} 图谱清理失败（不影响删除主流程）: {e}")
    # 批次2-4：同步清理审批规则（文档删了规则不留；失败只留日志）
    try:
        from app.rag.rules import service as rules_service

        removed = await rules_service.delete_rules_for_document(doc_id)
        if removed:
            logger.info(f"文档 {doc_id} 审批规则清理完成（删除 {removed} 条）")
    except Exception as e:
        logger.warning(f"文档 {doc_id} 审批规则清理失败（不影响删除主流程）: {e}")
    # 批次11a：同步清理 RAPTOR 摘要树簇表（Chroma 摘要块已随上方的
    # delete_document_from_store 按 document_id 一并删除）
    try:
        from app.rag import raptor

        removed = await raptor.delete_tree_for_document(doc_id)
        if removed:
            logger.info(f"文档 {doc_id} RAPTOR 簇关系清理完成（删除 {removed} 条）")
    except Exception as e:
        logger.warning(f"文档 {doc_id} RAPTOR 簇表清理失败（不影响删除主流程）: {e}")

    logger.info(
        f"文档 {doc_id} ({doc.original_name}) 删除完成: "
        f"向量分块 {deleted_chunks} 个, 物理文件 + 数据库记录已清除"
    )


async def get_categories(
    db: AsyncSession, kb_ids: list[str] | None = None
) -> list[CategoryResponse]:
    """获取分类树（企业知识库阶段1：分类归属知识库，kb_ids 为可见库范围）"""
    query = select(KnowledgeCategory)
    if kb_ids is not None:
        query = query.where(KnowledgeCategory.kb_id.in_(kb_ids))
    result = await db.execute(query)
    categories = result.scalars().all()

    # 构建树形结构
    cat_map: dict[str, CategoryResponse] = {}
    roots: list[CategoryResponse] = []

    for cat in categories:
        resp = CategoryResponse(
            id=cat.id,
            name=cat.name,
            description=cat.description,
            parent_id=cat.parent_id,
            kb_id=cat.kb_id,
            children=[],
            created_at=cat.created_at,
        )
        cat_map[cat.id] = resp

    for cat in categories:
        resp = cat_map[cat.id]
        if resp.parent_id and resp.parent_id in cat_map:
            cat_map[resp.parent_id].children.append(resp)
        else:
            roots.append(resp)

    return roots


async def create_category(
    db: AsyncSession, data: CategoryCreate, kb_id: str
) -> CategoryResponse:
    """创建分类（企业知识库阶段1：分类归属知识库；验收批次B：同一父分类下名称唯一）"""
    kb_result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
    )
    if not kb_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="知识库不存在")
    if data.parent_id:
        parent_result = await db.execute(
            select(KnowledgeCategory).where(
                KnowledgeCategory.id == data.parent_id,
                KnowledgeCategory.kb_id == kb_id,
            )
        )
        if not parent_result.scalar_one_or_none():
            raise HTTPException(
                status_code=400, detail="父分类不存在或不属于该知识库"
            )

    # 验收批次B：服务层同名查重——PG 唯一约束对 NULL parent_id 视为互异，
    # 顶级分类的同名重复失去数据库级拦截，须在应用层补上
    dup_cond = (
        (KnowledgeCategory.kb_id == kb_id)
        & (KnowledgeCategory.name == data.name)
        & (
            KnowledgeCategory.parent_id.is_(None)
            if not data.parent_id
            else (KnowledgeCategory.parent_id == data.parent_id)
        )
    )
    dup_result = await db.execute(
        select(KnowledgeCategory.id).where(dup_cond)
    )
    if dup_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="同一父分类下已存在同名分类")

    category = KnowledgeCategory(
        name=data.name,
        description=data.description,
        parent_id=data.parent_id,
        kb_id=kb_id,
    )
    db.add(category)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="同一父分类下已存在同名分类")
    await db.refresh(category)
    return CategoryResponse(
        id=category.id, name=category.name,
        description=category.description, parent_id=category.parent_id,
        kb_id=category.kb_id,
        children=[], created_at=category.created_at,
    )


async def delete_category(db: AsyncSession, category_id: str) -> None:
    """删除分类（批次F 验收修复：企业生产护栏 + 级联删除）

    - 分类及其全部子孙分类下还有文档 → 400 拒绝（防批量文档变孤儿；
      验收实测：父分类下有文档时用户先删文档或移动文档再删分类）
    - 无文档 → 父分类与子分类一并级联删除（验收实测：只删父分类会
      留下孤儿子分类悬在分类树顶层）
    """
    result = await db.execute(
        select(KnowledgeCategory).where(KnowledgeCategory.id == category_id)
    )
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="分类不存在")

    # 收集该分类 + 全部子孙分类（树形向下收集，防未来加深）
    cat_scope: list[str] = [category_id]
    frontier = [category_id]
    while frontier:
        kids = (await db.execute(
            select(KnowledgeCategory.id).where(
                KnowledgeCategory.parent_id.in_(frontier)
            )
        )).scalars().all()
        cat_scope.extend(kids)
        frontier = list(kids)

    # 护栏：分类树下有文档 → 拒绝删除
    doc_count = (await db.execute(
        select(func.count(Document.id)).where(Document.category_id.in_(cat_scope))
    )).scalar() or 0
    if doc_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"该分类及其子分类下还有 {doc_count} 篇文档，请先移动或删除文档后再删除分类",
        )

    # 无文档：级联删除（先删子分类再删父分类，防外键约束）
    if len(cat_scope) > 1:
        for cid in cat_scope[1:]:
            kid = (await db.execute(
                select(KnowledgeCategory).where(KnowledgeCategory.id == cid)
            )).scalar_one_or_none()
            if kid is not None:
                await db.delete(kid)
    await db.delete(cat)
    await db.flush()


async def get_document_chunks(
    db: AsyncSession,
    doc_id: str,
    around: int | None = None,
    radius: int = 1,
) -> ChunkListResponse:
    """获取文档的分块；around 指定时只返回该块附近的切片（引用"查看上下文"用）。

    total 始终是文档全部分块数——前端据此展示"第 N / M 块"定位信息。
    """
    # 先确认文档存在
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 从 ChromaDB 获取分块
    chunks_data = get_chunks_by_document(doc_id)
    total = len(chunks_data)
    if around is not None:
        lo, hi = around - radius, around + radius
        # int() 归一化：防御 Chroma 历史数据中 chunk_index 可能为字符串（比较不归一化会 TypeError）
        chunks_data = [c for c in chunks_data if lo <= int(c["chunk_index"]) <= hi]
    chunks = [
        ChunkResponse(
            chunk_id=c["chunk_id"],
            content=c["content"],
            chunk_index=c["chunk_index"],
            chunk_hash=c["chunk_hash"],
            filename=c["filename"],
            category_id=c["category_id"],
            parent_category_id=c.get("parent_category_id", ""),
            chunk_type=c.get("chunk_type", ""),
        )
        for c in chunks_data
    ]
    return ChunkListResponse(document_id=doc_id, total=total, chunks=chunks)


async def get_stats(
    db: AsyncSession, kb_ids: list[str] | None = None, kb_id: str | None = None
) -> KnowledgeStats:
    """获取知识库统计（P2-4 修复：只读，不再附带孤立分块清理副作用。

    孤立清理改由显式的 POST /api/v1/knowledge/cleanup-orphans 触发，见 cleanup_orphans()。
    GET 请求不应产生删除行为。

    企业知识库阶段1：kb_id 指定单库统计；kb_ids 为可见库范围（kb_id 优先）。
    """
    if kb_id:
        kb_ids = [kb_id]
    doc_where = []
    if kb_ids is not None:
        doc_where.append(Document.kb_id.in_(kb_ids))

    result = await db.execute(
        select(
            func.count(Document.id),
            func.sum(Document.chunk_count),
            func.sum(Document.file_size),
        ).where(*doc_where)
    )
    total, chunks, total_size = result.one()

    # 按状态统计
    completed_result = await db.execute(
        select(func.count(Document.id)).where(
            Document.status == "completed", *doc_where
        )
    )
    completed = completed_result.scalar() or 0

    processing_result = await db.execute(
        select(func.count(Document.id)).where(
            Document.status == "processing", *doc_where
        )
    )
    processing = processing_result.scalar() or 0

    failed_result = await db.execute(
        select(func.count(Document.id)).where(
            Document.status == "failed", *doc_where
        )
    )
    failed = failed_result.scalar() or 0

    cat_query = select(func.count(KnowledgeCategory.id))
    if kb_ids is not None:
        cat_query = cat_query.where(KnowledgeCategory.kb_id.in_(kb_ids))
    cat_result = await db.execute(cat_query)
    cat_count = cat_result.scalar() or 0

    # 各分类文档数统计
    cat_counts_query = (
        select(KnowledgeCategory.name, func.count(Document.id))
        .outerjoin(Document, Document.category_id == KnowledgeCategory.id)
    )
    if kb_ids is not None:
        cat_counts_query = cat_counts_query.where(KnowledgeCategory.kb_id.in_(kb_ids))
    cat_counts_result = await db.execute(
        cat_counts_query.group_by(KnowledgeCategory.id, KnowledgeCategory.name)
    )
    category_document_counts = {
        row[0]: row[1] for row in cat_counts_result if row[0]
    }

    return KnowledgeStats(
        total_documents=total or 0,
        total_chunks=chunks or 0,
        completed_documents=completed,
        processing_documents=processing,
        failed_documents=failed,
        total_size_bytes=total_size or 0,
        categories_count=cat_count,
        category_document_counts=category_document_counts,
        kb_id=kb_id,
    )


async def cleanup_orphans(db: AsyncSession) -> int:
    """清理 ChromaDB 中无对应 SQL 记录的孤立分块（P2-4：显式管理操作）。

    返回清理的分块数量。全库扫描 + 删除是重操作，放线程池执行。
    """
    from app.rag.retriever import cleanup_orphan_chunks

    valid_docs_result = await db.execute(select(Document.id))
    valid_ids = {row[0] for row in valid_docs_result}
    return await asyncio.to_thread(cleanup_orphan_chunks, valid_ids)
