"""SSO 登录插槽测试（企业知识库改造 阶段4，用内置 test provider 验证全流程）"""
import pytest
from httpx import AsyncClient

from app.config import settings


@pytest.fixture
def enable_test_sso(monkeypatch):
    monkeypatch.setattr(settings, "sso_test_enabled", True)
    yield


@pytest.mark.asyncio
async def test_providers_empty_when_disabled(client: AsyncClient):
    """未配置任何 provider：列表为空"""
    resp = await client.get("/api/v1/auth/sso/providers")
    assert resp.status_code == 200
    assert resp.json()["providers"] == []


@pytest.mark.asyncio
async def test_test_provider_full_flow(client: AsyncClient, enable_test_sso):
    """插槽全流程：providers → url（state 签名）→ callback → 自动建号+挂部门 → 登录可用"""
    # providers 出现 test
    resp = await client.get("/api/v1/auth/sso/providers")
    assert [p["name"] for p in resp.json()["providers"]] == ["test"]

    # 拿授权链接（test provider 直接指向回调）
    resp = await client.get("/api/v1/auth/sso/test/url")
    assert resp.status_code == 200
    auth_url = resp.json()["auth_url"]
    assert "/sso/test/callback" in auth_url

    # 提取 state 与 code（test provider 的链接里带预置 code：张三 / 人事部）
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(auth_url).query)
    state = q["state"][0]
    code = q["code"][0]

    # 直接调 callback（真实流程是企业 IM 带 code 回跳，这里等价）
    resp = await client.get(
        "/api/v1/auth/sso/test/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/sso-callback#" in location
    # token 在 fragment 里（不进访问日志）
    from urllib.parse import urlparse, parse_qs
    fragment = parse_qs(urlparse(location).fragment)
    access_token = fragment["access_token"][0]
    assert fragment["refresh_token"][0]

    # 自动建号：用户存在、挂到了"人事部"
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 200
    me = resp.json()
    assert me["role"] == "user"
    assert me["must_change_password"] is False

    # 部门自动创建并挂载
    from app.infrastructure.database import async_session_factory
    from app.models.knowledge_base import Department, UserDepartment
    from sqlalchemy import select
    async with async_session_factory() as db:
        dept = (await db.execute(
            select(Department).where(Department.name == "人事部")
        )).scalar_one_or_none()
        assert dept is not None
        binding = (await db.execute(
            select(UserDepartment).where(
                UserDepartment.user_id == me["id"],
                UserDepartment.department_id == dept.id,
            )
        )).scalar_one_or_none()
        assert binding is not None

    # 再次登录：同一身份不再建号（username 不变）
    resp = await client.get(
        "/api/v1/auth/sso/test/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    from app.infrastructure.database import async_session_factory as af
    from app.models.sso import UserSSOIdentity
    async with af() as db:
        count = (await db.execute(select(UserSSOIdentity))).scalars().all()
        assert len(count) == 1  # 唯一绑定，无重复建号


@pytest.mark.asyncio
async def test_callback_rejects_bad_state(client: AsyncClient, enable_test_sso):
    """伪造/过期 state → 400（CSRF 防护）"""
    resp = await client.get(
        "/api/v1/auth/sso/test/callback",
        params={"code": "test:x:y:", "state": "forged-state"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_disabled_provider_404(client: AsyncClient):
    """未启用的 provider（如未配密钥的企微）→ 404，不泄露存在性"""
    resp = await client.get("/api/v1/auth/sso/wecom/url")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bad_test_code_400(client: AsyncClient, enable_test_sso):
    """test code 格式错误 → 400"""
    resp = await client.get("/api/v1/auth/sso/test/url")
    from urllib.parse import urlparse, parse_qs
    state = parse_qs(urlparse(resp.json()["auth_url"]).query)["state"][0]
    resp = await client.get(
        "/api/v1/auth/sso/test/callback",
        params={"code": "not-a-test-code", "state": state},
    )
    assert resp.status_code == 400
