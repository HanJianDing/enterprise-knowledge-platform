"""SSO 登录插槽（企业知识库改造 阶段4）

把"生成授权链接 → 回调换身份 → 绑定或自动建号 → 发 token"抽象成
provider 接口（插槽）。以后接任何身份源（企业微信/钉钉/飞书/Keycloak…）
只需实现一个 provider 并配置密钥，账号体系与权限体系都不用动。

流程（所有 provider 同构）：
1. 前端 GET /auth/sso/providers 拿已启用的 provider 列表（没配密钥的不显示）
2. 用户点击 → GET /auth/sso/{provider}/url 拿授权链接（state 用 JWT 签名防 CSRF）
3. 用户在企业 IM 里确认 → 浏览器带 code 回跳 /auth/sso/{provider}/callback
4. 服务端用 code 换身份 → 有绑定直接登录；无绑定自动建号 + 按部门名挂载 +
   写审计 → 302 重定向前端 /sso-callback，token 放 URL fragment（# 后，不进访问日志）

自动建号策略说明（设计取舍，非疏漏）：SSO 的身份源（企业微信/钉钉等）本身
就是企业的在职名册——能完成扫码/授权的即在编人员，视为"建号已被企业批准"。
若需更严格的准入（如只允许特定部门），可在此处加允许名单校验。
"""
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from fastapi import HTTPException
from jose import jwt
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.middleware.auth import create_access_token, create_refresh_token, hash_password
from app.models.knowledge_base import Department, UserDepartment
from app.models.sso import UserSSOIdentity
from app.models.user import User
from app.services import audit_service

STATE_TTL_SECONDS = 600  # state 签名有效期 10 分钟（防 CSRF；服务端不存 nonce，防重放靠短时效+签名）


# ─────────────────────────── Provider 接口（插槽） ───────────────────────────

@dataclass
class SSOUserInfo:
    """provider 换回来的员工身份"""
    openid: str            # provider 侧唯一标识
    name: str | None       # 姓名
    department_names: list[str] = field(default_factory=list)  # 部门名列表（挂载/同步用）
    raw: dict | None = None  # provider 原始返回（快照存档，排障用）


class SSOProvider(Protocol):
    name: str

    def build_auth_url(self, state: str) -> str: ...

    async def exchange_code(self, code: str) -> SSOUserInfo: ...


# ─────────────────────────── state 签名（防 CSRF） ───────────────────────────

def _sign_state(provider: str) -> str:
    payload = {
        "provider": provider,
        "nonce": secrets.token_urlsafe(16),
        "exp": int(time.time()) + STATE_TTL_SECONDS,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _verify_state(provider: str, state: str) -> None:
    """校验 state 签名/有效期/一致性（CSRF 防护：攻击者无法伪造回调）"""
    try:
        payload = jwt.decode(state, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except Exception:
        raise HTTPException(status_code=400, detail="登录状态已过期，请重新发起登录")
    if payload.get("provider") != provider:
        raise HTTPException(status_code=400, detail="登录状态不匹配，请重新发起登录")


# ─────────────────────────── Provider 实现 ───────────────────────────

class WeComProvider:
    """企业微信网页授权（扫码登录）。

    启用条件：WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_SECRET 三项齐全。
    注意：需企业微信管理后台创建"网页应用"，回调域名必须已备案 + HTTPS。
    """

    name = "wecom"

    def __init__(self, corp_id: str, agent_id: str, secret: str, redirect_uri: str):
        self._corp_id = corp_id
        self._agent_id = agent_id
        self._secret = secret
        self._redirect_uri = redirect_uri

    def build_auth_url(self, state: str) -> str:
        return (
            "https://open.weixin.qq.com/connect/oauth2/authorize"
            f"?appid={self._corp_id}&redirect_uri={self._redirect_uri}"
            f"&response_type=code&scope=snsapi_base&state={state}#wechat_redirect"
        )

    async def exchange_code(self, code: str) -> SSOUserInfo:
        async with httpx.AsyncClient(timeout=10) as client:
            # 1) code → userid（需先取 access_token）
            token_resp = await client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": self._corp_id, "corpsecret": self._secret},
            )
            token_data = token_resp.json()
            if token_data.get("errcode") != 0:
                raise HTTPException(status_code=502, detail="企业微信授权失败（取票据）")
            access_token = token_data["access_token"]

            # 2) userid → 用户详情（含部门 ID；此处按"部门名快照"简化处理，
            #    正式启用时按部门 ID 映射到本系统部门，映射表见文档）
            info_resp = await client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo",
                params={"access_token": access_token, "code": code},
            )
            info = info_resp.json()
            if info.get("errcode") != 0:
                raise HTTPException(status_code=502, detail="企业微信授权失败（换身份）")
            userid = info.get("userid") or info.get("openid")
            if not userid:
                raise HTTPException(status_code=502, detail="企业微信授权失败（无身份）")
        return SSOUserInfo(openid=str(userid), name=None, department_names=[], raw=info)


class TestProvider:
    """测试插槽（仅 SSO_TEST_ENABLED=true 时启用）：验证插槽全流程无需真实企业账号。

    code 格式：test:<openid>:<姓名>:<部门名1|部门名2>（URL 编码后放进回调）
    """

    name = "test"

    def build_auth_url(self, state: str) -> str:
        return (
            f"{settings.frontend_base_url or 'http://localhost:5173'}"
            f"/api/v1/auth/sso/test/callback?state={state}&code=test%3Aalice%3A%E5%BC%A0%E4%B8%89%3A%E4%BA%BA%E4%BA%8B%E9%83%A8"
        )

    async def exchange_code(self, code: str) -> SSOUserInfo:
        # 格式校验：test:openid:name:dept1|dept2
        if not code.startswith("test:"):
            raise HTTPException(status_code=400, detail="测试 code 格式错误")
        parts = code.split(":", 3)
        if len(parts) < 3:
            raise HTTPException(status_code=400, detail="测试 code 格式错误")
        openid = parts[1]
        name = parts[2] or None
        depts = [d for d in (parts[3].split("|") if len(parts) > 3 else []) if d]
        if not openid:
            raise HTTPException(status_code=400, detail="测试 code 缺少 openid")
        return SSOUserInfo(openid=openid, name=name, department_names=depts, raw={"test": True})


def get_enabled_providers() -> list[SSOProvider]:
    """返回已配置（启用）的 provider 列表——没配密钥的不显示不启用"""
    providers: list[SSOProvider] = []
    if settings.sso_test_enabled:
        providers.append(TestProvider())
    if settings.wecom_corp_id and settings.wecom_agent_id and settings.wecom_secret:
        redirect = f"{settings.frontend_base_url or 'http://localhost:5173'}/api/v1/auth/sso/wecom/callback"
        providers.append(WeComProvider(
            settings.wecom_corp_id, settings.wecom_agent_id,
            settings.wecom_secret, redirect,
        ))
    return providers


def get_provider(name: str) -> SSOProvider:
    for p in get_enabled_providers():
        if p.name == name:
            return p
    raise HTTPException(status_code=404, detail="该登录方式未启用")


# ─────────────────────────── 登录编排（插槽共用） ───────────────────────────

async def login_or_create_user(db: AsyncSession, provider: str, info: SSOUserInfo) -> User:
    """绑定已有账号则直接返回；否则自动建号 + 按部门名挂载。"""
    # 已有绑定 → 直接登录
    result = await db.execute(
        select(UserSSOIdentity).where(
            UserSSOIdentity.provider == provider,
            UserSSOIdentity.openid == info.openid,
        )
    )
    identity = result.scalar_one_or_none()
    if identity:
        user_result = await db.execute(select(User).where(User.id == identity.user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=502, detail="绑定账号不存在，请联系管理员")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")
        return user

    # 首次登录：自动建号（用户名 = 姓名或 openid 前缀；无密码影子账号——
    # 哈希随机串，密码登录永远不可用）
    base_username = (info.name or f"{provider}_{info.openid}")[:30]
    username = base_username
    for _ in range(100):  # 用户名冲突时加序号
        dup = await db.execute(select(User).where(User.username == username))
        if not dup.scalar_one_or_none():
            break
        username = f"{base_username}_{uuid.uuid4().hex[:4]}"
    user = User(
        username=username,
        email=f"{info.openid}@{provider}.sso.local",
        hashed_password=hash_password(secrets.token_urlsafe(32)),  # 随机不可登录密码
        role="user",
        must_change_password=False,
    )
    db.add(user)
    await db.flush()

    from sqlalchemy.exc import IntegrityError

    # 绑定写入用 savepoint 隔离：同一 openid 并发首登时，第二个请求会撞
    # (provider, openid) 唯一约束——expunge 掉冲突对象后重查复用已有绑定，
    # 不给用户 500（多建的 User 为孤儿账号，无害且极罕见）。
    identity = UserSSOIdentity(
        user_id=user.id,
        provider=provider,
        openid=info.openid,
        name=info.name,
        department_names=info.department_names or None,
        raw=info.raw,
    )
    try:
        async with db.begin_nested():
            db.add(identity)
            await db.flush()
    except IntegrityError:
        db.expunge(identity)  # 关键：丢弃冲突对象，防后续 autoflush 重试失败的 INSERT
        existing = (await db.execute(
            select(UserSSOIdentity).where(
                UserSSOIdentity.provider == provider,
                UserSSOIdentity.openid == info.openid,
            )
        )).scalar_one_or_none()
        if existing is None:
            raise HTTPException(status_code=502, detail="登录状态冲突，请重新发起登录")
        bound = (await db.execute(
            select(User).where(User.id == existing.user_id)
        )).scalar_one_or_none()
        if bound is None:
            raise HTTPException(status_code=502, detail="绑定账号不存在，请联系管理员")
        if not bound.is_active:
            raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")
        return bound

    # 按部门名挂载：同名部门存在则绑定，不存在则自动创建（组织同步）。
    # 并发首登（两员工同部门同时扫码）会撞部门名唯一约束——
    # savepoint 隔离 + expunge 冲突对象后重查复用，不给用户 500。
    for dept_name in info.department_names:
        dept_result = await db.execute(
            select(Department).where(Department.name == dept_name)
        )
        dept = dept_result.scalar_one_or_none()
        if not dept:
            new_dept = Department(name=dept_name)
            try:
                async with db.begin_nested():
                    db.add(new_dept)
                    await db.flush()
                dept = new_dept
            except IntegrityError:
                db.expunge(new_dept)  # 丢弃冲突对象，防 autoflush 重试抛 PendingRollbackError
                dept_result = await db.execute(
                    select(Department).where(Department.name == dept_name)
                )
                dept = dept_result.scalar_one_or_none()
        if dept:
            db.add(UserDepartment(user_id=user.id, department_id=dept.id))
    await db.flush()
    await audit_service.record_audit(db, user.id, user.username, "user.sso_create",
        detail={"provider": provider, "openid": info.openid, "departments": info.department_names},
    )
    return user


async def complete_login(db: AsyncSession, user: User) -> dict:
    """登录收尾：顶号版本号原子 +1（与密码登录完全一致），签发双 token。"""
    from sqlalchemy import update as sa_update
    result = await db.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(session_version=User.session_version + 1)
        .returning(User.session_version)
    )
    sv = result.scalar_one()
    return {
        "access_token": create_access_token(user.id, user.role, sv),
        "refresh_token": create_refresh_token(user.id, sv),
        "must_change_password": False,
    }
