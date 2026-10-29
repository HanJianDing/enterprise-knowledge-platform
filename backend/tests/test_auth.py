"""认证模块测试（企业知识库改造阶段3：开放注册已关闭，建号走管理端）"""
import pytest
from httpx import AsyncClient

from tests.conftest import create_user_in_db


@pytest.mark.asyncio
async def test_register_endpoint_gone(client: AsyncClient):
    """开放注册已关闭：POST /auth/register 返回 404"""
    resp = await client.post("/api/v1/auth/register", json={
        "username": "testuser", "email": "test@test.com", "password": "123456"
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_login_success(client: AsyncClient):
    """测试正常登录"""
    await create_user_in_db("loginuser", "login@test.com")
    response = await client.post("/api/v1/auth/login", json={
        "username": "loginuser", "password": "123456"
    })
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["must_change_password"] is False  # 常规账号无首登改密要求


@pytest.mark.asyncio
async def test_login_must_change_password_flag(client: AsyncClient):
    """管理员建号的账号：登录返回 must_change_password=True（前端据此跳改密页）"""
    await create_user_in_db("newbie", "newbie@test.com", must_change_password=True)
    resp = await client.post("/api/v1/auth/login", json={
        "username": "newbie", "password": "123456"
    })
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True


@pytest.mark.asyncio
async def test_must_change_password_blocks_chat(client: AsyncClient):
    """未改初始密码不能提问（403）；改密后放行"""
    await create_user_in_db("forced_pw", "forced@test.com", must_change_password=True)
    login = await client.post("/api/v1/auth/login", json={
        "username": "forced_pw", "password": "123456"
    })
    token = login.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/api/v1/chat/stream", headers=H,
                             json={"message": "年假怎么申请？", "stream": True})
    assert resp.status_code == 403
    assert "修改初始密码" in resp.json()["detail"]

    # 改密后标记清除
    resp = await client.post("/api/v1/auth/change-password", headers=H, json={
        "old_password": "123456", "new_password": "newpass123"
    })
    assert resp.status_code == 200
    resp = await client.get("/api/v1/auth/me", headers=H)
    assert resp.status_code == 401  # 改密踢下线（sv+1），重新登录

    login2 = await client.post("/api/v1/auth/login", json={
        "username": "forced_pw", "password": "newpass123"
    })
    assert login2.json()["must_change_password"] is False
    resp = await client.get("/api/v1/auth/me",
                            headers={"Authorization": f"Bearer {login2.json()['access_token']}"})
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is False


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient):
    """测试错误密码登录"""
    await create_user_in_db("wrongpw", "wrong@test.com")
    response = await client.post("/api/v1/auth/login", json={
        "username": "wrongpw", "password": "wrongpassword"
    })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_nonexistent_user(client: AsyncClient):
    """测试不存在的用户登录"""
    response = await client.post("/api/v1/auth/login", json={
        "username": "nobody", "password": "123456"
    })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me(admin_client: AsyncClient):
    """测试获取当前用户信息"""
    response = await admin_client.get("/api/v1/auth/me")
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "admin_fixture"
    assert data["role"] == "admin"
    assert data["must_change_password"] is False


@pytest.mark.asyncio
async def test_change_password(admin_client: AsyncClient):
    """测试修改密码"""
    response = await admin_client.post("/api/v1/auth/change-password", json={
        "old_password": "123456", "new_password": "newpass123"
    })
    assert response.status_code == 200
    assert response.json()["message"] == "密码修改成功"

    # 用新密码登录
    response = await admin_client.post("/api/v1/auth/login", json={
        "username": "admin_fixture", "password": "newpass123"
    })
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_change_password_wrong_old(client: AsyncClient, admin_token: str):
    """测试旧密码错误时修改密码"""
    client.headers["Authorization"] = f"Bearer {admin_token}"
    response = await client.post("/api/v1/auth/change-password", json={
        "old_password": "wrongpassword", "new_password": "newpass123"
    })
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_change_password_kicks_old_tokens(client: AsyncClient):
    """上线安全修复：改密后旧 refresh 令牌立即失效（sv+1 踢全部旧令牌）"""
    await create_user_in_db("pw_kick", "kick@test.com")
    login = await client.post("/api/v1/auth/login", json={
        "username": "pw_kick", "password": "123456"
    })
    access = login.json()["access_token"]
    refresh = login.json()["refresh_token"]
    client.headers["Authorization"] = f"Bearer {access}"

    # 改密
    resp = await client.post("/api/v1/auth/change-password", json={
        "old_password": "123456", "new_password": "newpass123"
    })
    assert resp.status_code == 200

    # 旧 refresh 令牌 → 401（顶号文案）
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401
    assert "其他设备" in resp.json()["detail"]

    # 旧 access 令牌 → 401（sv 校验不通过）
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401

    # 新密码可正常登录
    resp = await client.post("/api/v1/auth/login", json={
        "username": "pw_kick", "password": "newpass123"
    })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_login_long_password_401_not_500(client: AsyncClient):
    """bcrypt 72 字节上限：超长密码登录 → 401（此前 verify 抛 ValueError 500）"""
    resp = await client.post("/api/v1/auth/login", json={
        "username": "nobody", "password": "密" * 30,
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unauthenticated_access(client: AsyncClient):
    """测试未认证访问受保护接口"""
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_invalid_token(client: AsyncClient):
    """测试无效Token"""
    client.headers["Authorization"] = "Bearer invalid_token"
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_body(client: AsyncClient):
    """安全修复回归：refresh token 走请求体（不再走 URL 查询参数）"""
    await create_user_in_db("refreshuser", "refresh@test.com")
    login = await client.post("/api/v1/auth/login", json={
        "username": "refreshuser", "password": "123456"
    })
    refresh_token = login.json()["refresh_token"]

    response = await client.post("/api/v1/auth/refresh", json={
        "refresh_token": refresh_token,
    })
    assert response.status_code == 200
    assert "access_token" in response.json()

    response = await client.post(
        "/api/v1/auth/refresh", params={"refresh_token": refresh_token}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_refresh_rejects_access_token(client: AsyncClient):
    """用 access token 冒充 refresh token → 401"""
    await create_user_in_db("refmisuse", "refmisuse@test.com")
    login = await client.post("/api/v1/auth/login", json={
        "username": "refmisuse", "password": "123456"
    })
    access_token = login.json()["access_token"]

    response = await client.post("/api/v1/auth/refresh", json={
        "refresh_token": access_token,
    })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_kick_old_session_on_relogin(client: AsyncClient):
    """顶号回归：同一账号第二次登录后，第一次的 access token 失效（401 提示其他设备）"""
    await create_user_in_db("kickuser", "kick@test.com")
    login1 = await client.post("/api/v1/auth/login", json={
        "username": "kickuser", "password": "123456"
    })
    token1 = login1.json()["access_token"]

    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token1}"})
    assert resp.status_code == 200

    login2 = await client.post("/api/v1/auth/login", json={
        "username": "kickuser", "password": "123456"
    })
    token2 = login2.json()["access_token"]
    assert token2 != token1

    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token2}"})
    assert resp.status_code == 200
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token1}"})
    assert resp.status_code == 401
    assert "其他设备" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_old_refresh_token_rejected_after_relogin(client: AsyncClient):
    """顶号回归：旧设备的 refresh token 在重新登录后不能换新票"""
    await create_user_in_db("kickrefresh", "kickrefresh@test.com")
    login1 = await client.post("/api/v1/auth/login", json={
        "username": "kickrefresh", "password": "123456"
    })
    refresh1 = login1.json()["refresh_token"]

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh1})
    assert resp.status_code == 200

    await client.post("/api/v1/auth/login", json={
        "username": "kickrefresh", "password": "123456"
    })
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh1})
    assert resp.status_code == 401
    assert "其他设备" in resp.json()["detail"]
