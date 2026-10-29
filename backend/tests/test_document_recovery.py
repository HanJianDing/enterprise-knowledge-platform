"""阶段6 测试：上传任务持久化（重启恢复 / 失败重试 / in-flight 去重）"""
import asyncio
import os
import time

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.infrastructure.database import async_session_factory
from app.models.document import Document


async def _drain_processing_tasks(timeout: float = 10.0) -> None:
    """等测试中触发的后台处理任务自然结束（无密钥时嵌入很快失败），
    防止任务残留到下一个测试，与 drop_all/create_all 打架。"""
    from app.services import document_service

    deadline = time.monotonic() + timeout
    while document_service._in_flight_doc_ids and time.monotonic() < deadline:
        await asyncio.sleep(0.2)


async def _seed_failed_doc(kb_id: str, uploader_id: str) -> tuple[str, str]:
    """落库一个 failed 文档 + 真实物理文件（reprocess 需要文件存在）"""
    filename = "recovery_test.txt"
    path = os.path.join(settings.upload_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write("阶段6 重试测试内容")
    async with async_session_factory() as db:
        doc = Document(
            filename=filename, original_name=filename, file_type="txt",
            file_size=10, storage_path=path, kb_id=kb_id,
            content_type="long_text", status="failed", chunk_count=0,
            uploaded_by=uploader_id,
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        return doc.id, path


@pytest.mark.asyncio
async def test_reprocess_failed_document(admin_client: AsyncClient, default_kb: str):
    """失败文档重试：200、状态重置回处理队列；物理文件不存在时 400"""
    from tests.conftest import create_user_in_db
    from app.models.user import User

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.role == "admin").limit(1))
        admin_id = result.scalar_one().id

    doc_id, path = await _seed_failed_doc(default_kb, admin_id)

    # 普通用户无 can_upload → 403
    await create_user_in_db("retry_peeper", "retryp@test.com")
    resp = await admin_client.post("/api/v1/auth/login", json={
        "username": "retry_peeper", "password": "123456"
    })
    resp = await admin_client.post(
        f"/api/v1/knowledge/documents/{doc_id}/reprocess",
        headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
    )
    assert resp.status_code == 403

    # admin 重试 → 200，状态从 failed 重置（后台任务随后处理）
    resp = await admin_client.post(f"/api/v1/knowledge/documents/{doc_id}/reprocess")
    assert resp.status_code == 200
    await _drain_processing_tasks()  # 等任务处理完，状态确定（A1 后任务多一轮流水写入，时序更靠后）
    async with async_session_factory() as db:
        doc = (await db.execute(
            select(Document).where(Document.id == doc_id)
        )).scalar_one()
        assert doc.status in ("uploaded", "processing", "failed", "completed")

    # 非失败状态不可重试（质量审查 MEDIUM 修复：completed 反复重试白烧嵌入成本；
    # 产品化 A1：completed 需 force=true 才能"重新分块"，默认拒绝）
    resp = await admin_client.post(f"/api/v1/knowledge/documents/{doc_id}/reprocess")
    assert resp.status_code == 400
    assert "force=true" in resp.json()["detail"]

    # 物理文件不存在 → 400（先把状态改回 failed）
    os.remove(path)
    async with async_session_factory() as db:
        doc = (await db.execute(
            select(Document).where(Document.id == doc_id)
        )).scalar_one()
        doc.status = "failed"
        await db.commit()
    resp = await admin_client.post(f"/api/v1/knowledge/documents/{doc_id}/reprocess")
    assert resp.status_code == 400
    assert "物理文件" in resp.json()["detail"]

    # 不存在的文档 → 404
    resp = await admin_client.post("/api/v1/knowledge/documents/not-exist/reprocess")
    assert resp.status_code == 404

    await _drain_processing_tasks()


@pytest.mark.asyncio
async def test_recover_processing_documents(default_kb: str):
    """启动恢复：卡在 uploaded/processing 的文档被重新入队（返回恢复数量）"""
    from app.models.user import User
    from app.services import document_service

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.role == "admin").limit(1))
        admin_id = result.scalar_one().id
        path = os.path.join(settings.upload_dir, "stuck_test.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("卡住文档内容")
        for status in ("uploaded", "processing"):
            db.add(Document(
                filename=f"stuck_{status}.txt", original_name=f"stuck_{status}.txt",
                file_type="txt", file_size=10, storage_path=path, kb_id=default_kb,
                content_type="long_text", status=status, chunk_count=0,
                uploaded_by=admin_id,
            ))
        await db.commit()

    recovered = await document_service.recover_processing_documents()
    assert recovered >= 2  # 两条卡住的文档都被重新入队

    # 再跑一次：in-flight 去重（已在处理的不会被重复入队）
    second = await document_service.recover_processing_documents()
    assert second == 0

    await _drain_processing_tasks()

    # 清理测试文件
    os.remove(os.path.join(settings.upload_dir, "stuck_test.txt"))


@pytest.mark.asyncio
async def test_spawn_dedupe():
    """spawn_processing_task 对同一文档去重（并发安全）"""
    from app.services import document_service

    path = os.path.join(settings.upload_dir, "dedupe_test.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("去重测试")
    document_service.spawn_processing_task("doc-dedupe-1", path, "dedupe_test.txt")
    # 立刻第二次入队同一文档 → 被跳过
    document_service.spawn_processing_task("doc-dedupe-1", path, "dedupe_test.txt")
    assert "doc-dedupe-1" in document_service._in_flight_doc_ids

    await _drain_processing_tasks()
    os.remove(path)
