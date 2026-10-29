"""批次F 前置·diff 视图测试：版本链/版本内容/回溯端点"""
import os

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.infrastructure.database import async_session_factory
from app.models.document import Document
from app.models.user import User
from app.services import document_service


async def _seed_versioned_doc(kb_id: str) -> tuple[str, str]:
    """落库两条版本链文档（v2 active + v1 superseded）+ 真实物理文件"""
    async with async_session_factory() as db:
        admin = (await db.execute(
            select(User).where(User.role == "admin").limit(1)
        )).scalar_one()
        v1_path = os.path.join(settings.upload_dir, "ver_v1.txt")
        v2_path = os.path.join(settings.upload_dir, "ver_v2.txt")
        with open(v1_path, "w", encoding="utf-8") as f:
            f.write("第一条 上班时间 9 点")
        with open(v2_path, "w", encoding="utf-8") as f:
            f.write("第一条 上班时间 9 点\n第二条 下班时间 17 点")
        v1 = Document(filename="v1", original_name="考勤管理制度.txt",
                      file_type="txt", file_size=10, storage_path=v1_path,
                      kb_id=kb_id, content_type="long_text",
                      status="superseded", chunk_count=1, version_no=1,
                      uploaded_by=admin.id)
        db.add(v1)
        await db.flush()
        v2 = Document(filename="v2", original_name="考勤管理制度.txt",
                      file_type="txt", file_size=10, storage_path=v2_path,
                      kb_id=kb_id, content_type="long_text",
                      status="completed", chunk_count=2, version_no=2,
                      version_of=v1.id, uploaded_by=admin.id)
        db.add(v2)
        await db.commit()
        return v2.id, v1.id


@pytest.mark.asyncio
async def test_version_chain_and_content(admin_client: AsyncClient, default_kb):
    """版本链（旧版在前）+ 各版本内容提取（diff 数据源）"""
    doc_id, v1_id = await _seed_versioned_doc(default_kb)

    chain = (await admin_client.get(
        f"/api/v1/admin/documents/{doc_id}/versions")).json()
    assert [v["version_no"] for v in chain] == [1, 2]
    assert chain[0]["status"] == "superseded"
    assert chain[0]["id"] == v1_id

    # 批次F 前置修复回归：从中间旧版打开版本链，down 反查同样收全
    # （修复前共享 seen 集合让 down 分支失效，旧版打开会漏当前版）
    chain_from_v1 = (await admin_client.get(
        f"/api/v1/admin/documents/{v1_id}/versions")).json()
    assert [v["version_no"] for v in chain_from_v1] == [1, 2]

    c1 = (await admin_client.get(
        f"/api/v1/admin/documents/{v1_id}/content")).json()
    assert c1["version_no"] == 1
    assert "上班时间 9 点" in c1["content"]

    c2 = (await admin_client.get(
        f"/api/v1/admin/documents/{doc_id}/content")).json()
    assert c2["version_no"] == 2
    assert "下班时间 17 点" in c2["content"]


@pytest.mark.asyncio
async def test_content_requires_admin(user_client: AsyncClient, default_kb):
    """非管理员取版本内容 → 403（diff 是管理端功能）"""
    doc_id, _ = await _seed_versioned_doc(default_kb)
    r = await user_client.get(f"/api/v1/admin/documents/{doc_id}/content")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_content_missing_file_400(admin_client: AsyncClient, default_kb):
    """物理文件缺失 → 400 中文提示（reprocess 同款语义）"""
    doc_id, v1_id = await _seed_versioned_doc(default_kb)
    async with async_session_factory() as db:
        doc = (await db.execute(
            select(Document).where(Document.id == v1_id))).scalar_one()
        doc.storage_path = "/nonexistent/ver_v1.txt"
        await db.commit()
    r = await admin_client.get(
        f"/api/v1/admin/documents/{v1_id}/content")
    assert r.status_code == 400
    assert "物理文件已不存在" in r.json()["detail"]


@pytest.mark.asyncio
async def test_reprocess_keeps_superseded(admin_client: AsyncClient, default_kb):
    """批次F 验收修复回归：废止文档（有更新版本指向）重处理后保持
    superseded——重处理重置 status 会"复活"废止文档，其旧条款参与
    检索误导审批题（真实验收事故："报销 3000 元"被 2024 版规则
    劫持答成财务总监）"""
    doc_id, v1_id = await _seed_versioned_doc(default_kb)
    # 服务层直调（端点层对 superseded 已有 400 防线——真实验收事故是
    # Temp 脚本直调服务函数绕过了端点，此处测服务层纵深防御）
    async with async_session_factory() as db:
        await document_service.reprocess_document(db, v1_id, force=True)
    import asyncio as _a
    from app.services import document_service as _ds

    deadline = _a.get_running_loop().time() + 10
    while _ds._in_flight_doc_ids and _a.get_running_loop().time() < deadline:
        await _a.sleep(0.05)
    async with async_session_factory() as db:
        v1 = (await db.execute(
            select(Document).where(Document.id == v1_id))).scalar_one()
        v2 = (await db.execute(
            select(Document).where(Document.id == doc_id))).scalar_one()
        assert v1.status == "superseded"  # 修复前此处为 completed（复活）
        assert v2.status == "completed"


@pytest.mark.asyncio
async def test_restore_endpoint(admin_client: AsyncClient, default_kb):
    """回溯：旧版恢复为 completed，当前版标 superseded"""
    doc_id, v1_id = await _seed_versioned_doc(default_kb)
    r = await admin_client.post(
        f"/api/v1/admin/documents/{v1_id}/restore")
    assert r.status_code == 200
    assert r.json()["version_no"] == 1
    async with async_session_factory() as db:
        v1 = (await db.execute(
            select(Document).where(Document.id == v1_id))).scalar_one()
        v2 = (await db.execute(
            select(Document).where(Document.id == doc_id))).scalar_one()
        assert v1.status == "completed"
        assert v2.status == "superseded"
