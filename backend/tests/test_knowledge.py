"""知识库管理模块测试"""
from tests.conftest import create_user_in_db
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_knowledge_list_scoped_for_user(client: AsyncClient):
    """企业知识库阶段1：普通用户可以访问文档列表，但只能看到可见库范围（无授权即空列表）"""
    # 注册普通用户
    await create_user_in_db("normal", "normal@test.com", "123456")
    resp = await client.post("/api/v1/auth/login", json={
        "username": "normal", "password": "123456"
    })
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"

    # 无任何授权：文档列表可访问但为空
    resp = await client.get("/api/v1/knowledge/documents")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_knowledge_stats(admin_client: AsyncClient):
    """测试获取知识库统计"""
    resp = await admin_client.get("/api/v1/knowledge/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_documents" in data
    assert "total_chunks" in data
    assert "categories_count" in data


@pytest.mark.asyncio
async def test_knowledge_documents_empty(admin_client: AsyncClient):
    """测试空文档列表"""
    resp = await admin_client.get("/api/v1/knowledge/documents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_knowledge_categories_crud(admin_client: AsyncClient, default_kb: str):
    """测试分类 CRUD 操作（企业知识库阶段1：分类归属知识库）"""
    # 创建分类
    resp = await admin_client.post("/api/v1/knowledge/categories", json={
        "name": "手机配件", "description": "手机相关配件产品", "kb_id": default_kb
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "手机配件"
    assert data["kb_id"] == default_kb
    cat_id = data["id"]

    # 获取分类列表
    resp = await admin_client.get("/api/v1/knowledge/categories")
    assert resp.status_code == 200
    cats = resp.json()
    assert len(cats) >= 1
    assert any(c["name"] == "手机配件" for c in cats)

    # 删除分类
    resp = await admin_client.delete(f"/api/v1/knowledge/categories/{cat_id}")
    assert resp.status_code == 200
    assert "已删除" in resp.json()["message"]

    # 确认删除
    resp = await admin_client.get("/api/v1/knowledge/categories")
    cats = resp.json()
    assert not any(c["id"] == cat_id for c in cats)


@pytest.mark.asyncio
async def test_upload_invalid_type(admin_client: AsyncClient, default_kb: str):
    """测试上传不支持的文件类型"""
    files = {"file": ("test.exe", b"fake content", "application/octet-stream")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload", files=files, data={"kb_id": default_kb}
    )
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_txt_document(admin_client: AsyncClient, default_kb: str):
    """测试上传 TXT 文档（企业知识库阶段1：文档归属知识库）"""
    content = "iPhone 15 Pro Max 电池容量为 4422mAh，支持 27W 快充。"
    files = {"file": ("test_product.txt", content.encode("utf-8"), "text/plain")}
    resp = await admin_client.post(
        "/api/v1/knowledge/documents/upload", files=files, data={"kb_id": default_kb}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "processing"
    assert data["filename"] == "test_product.txt"

    # 验证文档已上传（不管处理状态，异步处理需要时间）
    resp = await admin_client.get("/api/v1/knowledge/documents")
    data = resp.json()
    assert data["total"] >= 1
    assert data["items"][0]["original_name"] == "test_product.txt"
    assert data["items"][0]["kb_id"] == default_kb
    assert data["items"][0]["status"] in ("uploaded", "processing", "completed")


@pytest.mark.asyncio
async def test_admin_dashboard(admin_client: AsyncClient):
    """测试管理仪表盘"""
    resp = await admin_client.get("/api/v1/admin/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert "user_count" in data
    assert "conversation_count" in data
    assert "document_count" in data


@pytest.mark.asyncio
async def test_admin_users_list(admin_client: AsyncClient):
    """测试用户列表"""
    resp = await admin_client.get("/api/v1/admin/users?page=1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert len(data["items"]) >= 1


@pytest.mark.asyncio
async def test_cleanup_orphans_requires_admin(client: AsyncClient, user_token: str):
    """P2-4 回归：普通用户不能调用孤儿清理接口"""
    resp = await client.post(
        "/api/v1/knowledge/cleanup-orphans",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cleanup_orphans_admin_ok(admin_client: AsyncClient):
    """P2-4 回归：admin 可显式触发孤儿清理，返回清理数量"""
    resp = await admin_client.post("/api/v1/knowledge/cleanup-orphans")
    assert resp.status_code == 200
    assert "已清理" in resp.json()["message"]


@pytest.mark.asyncio
async def test_stats_is_read_only(admin_client: AsyncClient):
    """P2-4 回归：GET /stats 只读，可正常返回统计"""
    resp = await admin_client.get("/api/v1/knowledge/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["total_documents"], int)
    assert isinstance(data["total_chunks"], int)
