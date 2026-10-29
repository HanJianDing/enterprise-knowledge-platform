"""产品化 A1 测试：存量文档重新分块（reprocess force=true）与 xlsx 上传"""
import asyncio
import os
import time

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.infrastructure.database import async_session_factory
from app.models.document import Document
from app.models.user import User
from app.services import document_service


async def _drain(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while document_service._in_flight_doc_ids and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


async def _seed_completed_doc(kb_id: str) -> tuple[str, str]:
    """落库一条 completed 文档 + 真实物理文件"""
    filename = "rechunk_test.txt"
    path = os.path.join(settings.upload_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write("第十二条 婚假\n员工结婚可享受婚假 3 天。")
    async with async_session_factory() as db:
        admin = (await db.execute(
            select(User).where(User.role == "admin").limit(1)
        )).scalar_one()
        doc = Document(
            filename=filename, original_name=filename, file_type="txt",
            file_size=10, storage_path=path, kb_id=kb_id,
            content_type="long_text", status="completed", chunk_count=1,
            uploaded_by=admin.id,
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        return doc.id, path


@pytest.mark.asyncio
async def test_reprocess_force_on_completed(admin_client: AsyncClient, default_kb: str):
    """已完成文档：force=true 重新分块 200 并入队；force 缺省 400 防白烧"""
    doc_id, _ = await _seed_completed_doc(default_kb)

    # 默认（无 force）：拒绝（守住"仅失败可重试"的质量修复）
    resp = await admin_client.post(f"/api/v1/knowledge/documents/{doc_id}/reprocess")
    assert resp.status_code == 400
    assert "force=true" in resp.json()["detail"]

    # force=true：200，状态重置回处理队列
    resp = await admin_client.post(
        f"/api/v1/knowledge/documents/{doc_id}/reprocess", params={"force": True}
    )
    assert resp.status_code == 200
    async with async_session_factory() as db:
        doc = (await db.execute(
            select(Document).where(Document.id == doc_id)
        )).scalar_one()
        assert doc.status in ("uploaded", "processing", "completed")
        # 重切事件写入流水
        assert any(e["stage"] == "reprocess" for e in (doc.process_log or []))

    await _drain()


@pytest.mark.asyncio
async def test_upload_xlsx_with_table_type(admin_client: AsyncClient, default_kb: str):
    """xlsx 文件 + content_type=table 上传通过校验（处理被测试桩替换）"""
    import io
    from openpyxl import Workbook

    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.append(["项目", "限额"])
    ws.append(["市内交通", "100元/天"])
    wb.save(buf)

    files = {
        "file": (
            "报销标准.xlsx",
            buf.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    }
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files,
        data={"kb_id": default_kb, "content_type": "table"},
    )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "报销标准.xlsx"

    await _drain()


@pytest.mark.asyncio
async def test_upload_unsupported_content_type(admin_client: AsyncClient, default_kb: str):
    """未知 content_type 仍被拒绝"""
    files = {"file": ("test.txt", b"hello", "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload",
        files=files,
        data={"kb_id": default_kb, "content_type": "ragflow"},
    )
    assert resp.status_code == 400
    assert "不支持的内容类型" in resp.json()["detail"]
