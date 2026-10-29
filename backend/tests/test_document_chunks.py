"""文档切片"上下文"接口测试（聊天页引用 → 查看原文上下文，企业产品化 A2）"""
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.infrastructure.database import async_session_factory
from app.models.document import Document
from app.models.user import User
from app.services import document_service


def _make_chunks(count: int = 5) -> list[dict]:
    """构造固定形状的分块数据（真实来源是 Chroma，这里打桩隔离）"""
    return [
        {
            "chunk_id": f"chunk-{i}",
            "content": f"第 {i} 块的原文内容",
            "chunk_index": i,
            "chunk_hash": f"hash-{i}",
            "filename": "测试制度.pdf",
            "category_id": "",
            "parent_category_id": "",
            "chunk_type": "long_text",
        }
        for i in range(count)
    ]


@pytest_asyncio.fixture
async def seeded_document(default_kb: str) -> str:
    """在 default_kb 下直落一条 completed 文档记录（绕过上传流程）"""
    doc_id = str(uuid.uuid4())
    async with async_session_factory() as db:
        admin = (await db.execute(
            select(User).where(User.role == "admin").limit(1)
        )).scalar_one()
        db.add(Document(
            id=doc_id, filename="test.pdf", original_name="测试制度.pdf",
            file_type="pdf", file_size=100, storage_path="test.pdf",
            kb_id=default_kb, content_type="long_text", status="completed",
            uploaded_by=admin.id,
        ))
        await db.commit()
    return doc_id


@pytest.mark.asyncio
async def test_document_chunks_around_filter(
    admin_client: AsyncClient, seeded_document: str, monkeypatch
):
    """around/radius 只返回附近切片，total 仍是全部分块数（前端"第 N / M 块"定位依赖）"""
    monkeypatch.setattr(
        document_service, "get_chunks_by_document", lambda doc_id: _make_chunks(5)
    )

    resp = await admin_client.get(
        f"/api/v1/knowledge/documents/{seeded_document}/chunks",
        params={"around": 2, "radius": 1},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert [c["chunk_index"] for c in data["chunks"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_document_chunks_without_around_returns_all(
    admin_client: AsyncClient, seeded_document: str, monkeypatch
):
    """不带 around 时返回全部分块（兼容原调用方，行为不变）"""
    monkeypatch.setattr(
        document_service, "get_chunks_by_document", lambda doc_id: _make_chunks(5)
    )

    resp = await admin_client.get(
        f"/api/v1/knowledge/documents/{seeded_document}/chunks"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["chunks"]) == 5


@pytest.mark.asyncio
async def test_document_chunks_nonexistent_document(admin_client: AsyncClient):
    """文档不存在返回 404"""
    resp = await admin_client.get(
        f"/api/v1/knowledge/documents/{uuid.uuid4()}/chunks"
    )
    assert resp.status_code == 404
