"""产品化 A3 测试：文档解析事件流水（documents.process_log）"""
import asyncio
import time

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.infrastructure.database import async_session_factory
from app.models.document import Document
from app.services import document_service


async def _drain(timeout: float = 5.0) -> None:
    """等后台处理任务自然结束（同 test_document_recovery 模式）"""
    deadline = time.monotonic() + timeout
    while document_service._in_flight_doc_ids and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


async def _fetch_doc(doc_id: str) -> Document:
    async with async_session_factory() as db:
        return (await db.execute(
            select(Document).where(Document.id == doc_id)
        )).scalar_one()


@pytest.mark.asyncio
async def test_process_log_happy_path(admin_client: AsyncClient, default_kb: str):
    """上传成功路径：流水按序包含 上传 → 开始处理 → 处理完成，事件带时间与详情"""
    content = "流程测试内容：年假申请需提前三天提交。"
    files = {"file": ("process_log_test.txt", content.encode("utf-8"), "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload", files=files, data={"kb_id": default_kb}
    )
    assert resp.status_code == 200
    doc_id = resp.json()["id"]

    await _drain()

    doc = await _fetch_doc(doc_id)
    assert doc.status == "completed"
    log = doc.process_log
    assert log is not None and len(log) >= 3
    stages = [e["stage"] for e in log]
    assert stages[0] == "upload"
    assert "start" in stages
    assert stages[-1] == "done"
    done_event = log[-1]
    assert "块" in done_event["detail"]  # 完成事件带分块数
    for e in log:
        assert e["time"]
        assert e["detail"]


@pytest.mark.asyncio
async def test_process_log_failure(
    admin_client: AsyncClient, default_kb: str, monkeypatch
):
    """处理失败路径：流水以 error 事件结尾，error_message 落库，列表接口带出"""
    from app.services import document_service as ds

    # 覆盖 conftest 的成功桩：让处理返回失败
    monkeypatch.setattr(ds, "process_document", lambda *a, **k: (0, "模拟解析崩溃"))

    content = "失败路径测试内容"
    files = {"file": ("process_log_fail.txt", content.encode("utf-8"), "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload", files=files, data={"kb_id": default_kb}
    )
    assert resp.status_code == 200
    doc_id = resp.json()["id"]

    await _drain()

    doc = await _fetch_doc(doc_id)
    assert doc.status == "failed"
    assert doc.error_message == "模拟解析崩溃"
    assert doc.process_log[-1]["stage"] == "error"
    assert "模拟解析崩溃" in doc.process_log[-1]["detail"]

    # 列表接口带出 process_log 与 error_message（前端解析流水弹窗依赖）
    resp = await admin_client.get(
        "/api/v1/knowledge/documents", params={"kb_id": default_kb}
    )
    item = next(i for i in resp.json()["items"] if i["id"] == doc_id)
    assert item["status"] == "failed"
    assert "模拟解析崩溃" in item["error_message"]
    assert item["process_log"][-1]["stage"] == "error"



@pytest.mark.asyncio
async def test_upload_encrypted_pdf_rejected_with_chinese_detail(
    admin_client: AsyncClient, default_kb: str
):
    """批次F 验收修复（3.9）：加密 PDF 上传时同步拦截——400 + 中文弹窗提示。
    此前只靠后台处理管道失败（错误进列表状态），上传本身"成功"、
    用户没有即时提示（真实验收：加密文档可上传，仅状态失败）"""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.encrypt(user_password="test123", owner_password="owner456")
    buf = io.BytesIO()
    writer.write(buf)

    files = {"file": ("加密文档.pdf", buf.getvalue(), "application/pdf")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload", files=files, data={"kb_id": default_kb}
    )
    assert resp.status_code == 400
    assert "已加密" in resp.json()["detail"]
    assert "解除密码" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_normal_pdf_not_rejected(
    admin_client: AsyncClient, default_kb: str
):
    """加密拦截不得误伤正常 PDF（回归护栏）"""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)

    files = {"file": ("正常文档.pdf", buf.getvalue(), "application/pdf")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload", files=files, data={"kb_id": default_kb}
    )
    assert resp.status_code == 200
