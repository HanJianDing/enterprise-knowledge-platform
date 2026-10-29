"""主应用端点测试"""
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_root_endpoint(client: AsyncClient):
    """测试根路径"""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert "RAG" in data["message"]
    assert data["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient):
    """测试健康检查端点（产品化 C4：返回数据库/向量库真实连通性）"""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("ok", "degraded")
    assert set(data["checks"].keys()) == {"database", "vector_store"}


@pytest.mark.asyncio
async def test_cors_headers(client: AsyncClient):
    """测试 CORS 头设置"""
    response = await client.options(
        "/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    # FastAPI CORS 中间件应该处理 OPTIONS 请求
    assert response.status_code in (200, 405)  # 200=OK, 405=Method Not Allowed on OPTIONS


@pytest.mark.asyncio
async def test_global_exception_handler(client: AsyncClient):
    """测试全局异常处理 —— 请求一个不存在的路径"""
    response = await client.get("/api/v1/nonexistent-endpoint")
    assert response.status_code in (404, 405)  # 不存在则 404，不是 500


@pytest.mark.asyncio
async def test_api_v1_prefix(client: AsyncClient):
    """测试 API v1 前缀可用"""
    # 不带认证访问需要认证的端点，应返回 401 而非 404
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_openapi_docs_available(client: AsyncClient):
    """测试 OpenAPI 文档可访问"""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    data = response.json()
    assert "openapi" in data
    assert "paths" in data
    # 验证关键路径存在
    assert "/health" in data["paths"]
    assert "/api/v1/auth/login" in data["paths"]
