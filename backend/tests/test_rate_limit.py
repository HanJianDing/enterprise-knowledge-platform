"""限频测试 — 限频器单测 + 端点 429 集成（上线安全修复）"""
import time

import pytest
from httpx import AsyncClient

from app.config import settings
from app.middleware.rate_limit import RateLimiter


class TestRateLimiter:
    """固定窗口计数器逻辑"""

    def test_allows_up_to_limit(self):
        rl = RateLimiter(window=60)
        for _ in range(5):
            assert rl.allow("k", 5) is True
        assert rl.allow("k", 5) is False

    def test_window_expiry_recovers(self):
        rl = RateLimiter(window=0.2)
        assert rl.allow("k", 1) is True
        assert rl.allow("k", 1) is False
        time.sleep(0.25)
        assert rl.allow("k", 1) is True

    def test_keys_isolated(self):
        rl = RateLimiter()
        for _ in range(3):
            rl.allow("a", 3)
        assert rl.allow("a", 3) is False
        assert rl.allow("b", 3) is True  # 不同 key 互不影响


@pytest.fixture
def enable_rate_limit(monkeypatch):
    """本测试文件内开启限频（其他文件由 conftest 关闭，互不干扰）"""
    from app.middleware.rate_limit import reset_limiter

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    reset_limiter()
    yield
    reset_limiter()


@pytest.mark.asyncio
async def test_login_429_after_limit(client: AsyncClient, enable_rate_limit):
    """登录：同 IP 超过 settings.login_rate_limit_per_min（默认 5）→ 429。
    阈值配置化（批次F 验收）：批次5.5 曾写死 30（压测账号误伤），
    2026-10-13 用户拍板恢复 5——测试按配置值跑，不写死数字"""
    from app.config import settings

    limit = settings.login_rate_limit_per_min
    for _ in range(limit):
        resp = await client.post("/api/v1/auth/login", json={
            "username": "nobody", "password": "wrongpass",
        })
        assert resp.status_code == 401
    resp = await client.post("/api/v1/auth/login", json={
        "username": "nobody", "password": "wrongpass",
    })
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_chat_429_after_10_per_user(client: AsyncClient, enable_rate_limit):
    """对话：同一用户第 11 次 → 429（企业改造阶段3：按用户限频，不再按 IP——
    企业出口 IP 收敛时按 IP 会把全公司一起限了）

    先造一个用户并登录（登录消耗 login 桶 1 次，无碍）；
    对话走 mock 链路不调用真实 LLM。
    """
    from app.middleware.auth import hash_password
    from app.models.user import User
    from app.infrastructure.database import async_session_factory
    from tests.test_role_response import _install_chat_mocks

    async with async_session_factory() as db:
        db.add(User(
            username="rl_chat_user", email="rlchat@test.com",
            hashed_password=hash_password("123456"), role="user",
        ))
        await db.commit()

    resp = await client.post("/api/v1/auth/login", json={
        "username": "rl_chat_user", "password": "123456",
    })
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    try:
        patches = _install_chat_mocks(["回答。"])
        for p in patches:
            p.start()
        try:
            for i in range(10):
                async with client.stream("POST", "/api/v1/chat/stream", json={
                    "message": f"问题{i}", "stream": True,
                }) as resp:
                    await resp.aread()
                    assert resp.status_code == 200
            # 第 11 次：限频依赖在鉴权之后、流式生成之前 → 429
            async with client.stream("POST", "/api/v1/chat/stream", json={
                "message": "超限问题", "stream": True,
            }) as resp:
                await resp.aread()
                assert resp.status_code == 429
        finally:
            for p in patches:
                p.stop()
    finally:
        client.headers.pop("Authorization", None)


@pytest.mark.asyncio
async def test_chat_limit_is_per_user_not_shared(client: AsyncClient, enable_rate_limit):
    """按用户限频：用户 A 用满配额后，同一 IP 的用户 B 不受影响（企业出口 IP 收敛场景）"""
    from app.middleware.auth import hash_password
    from app.models.user import User
    from app.infrastructure.database import async_session_factory
    from tests.test_role_response import _install_chat_mocks

    async with async_session_factory() as db:
        db.add(User(
            username="rl_a", email="rla@test.com",
            hashed_password=hash_password("123456"), role="user",
        ))
        db.add(User(
            username="rl_b", email="rlb@test.com",
            hashed_password=hash_password("123456"), role="user",
        ))
        await db.commit()

    patches = _install_chat_mocks(["回答。"])
    for p in patches:
        p.start()
    try:
        # A 用满 10 次 → 第 11 次 429
        resp = await client.post("/api/v1/auth/login", json={
            "username": "rl_a", "password": "123456"})
        token_a = resp.json()["access_token"]
        for i in range(11):
            async with client.stream("POST", "/api/v1/chat/stream", json={
                "message": f"A问题{i}", "stream": True,
            }, headers={"Authorization": f"Bearer {token_a}"}) as resp:
                await resp.aread()
                if i < 10:
                    assert resp.status_code == 200
                else:
                    assert resp.status_code == 429

        # B 同一 IP 第 1 次 → 正常 200（桶按用户隔离）
        resp = await client.post("/api/v1/auth/login", json={
            "username": "rl_b", "password": "123456"})
        token_b = resp.json()["access_token"]
        async with client.stream("POST", "/api/v1/chat/stream", json={
            "message": "B问题", "stream": True,
        }, headers={"Authorization": f"Bearer {token_b}"}) as resp:
            await resp.aread()
            assert resp.status_code == 200
    finally:
        for p in patches:
            p.stop()
