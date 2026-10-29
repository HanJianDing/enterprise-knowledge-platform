"""认证 API（企业知识库改造阶段3：开放注册已关闭，建号只走管理端）

阶段4：SSO 登录插槽——providers / url / callback 三个端点。
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from urllib.parse import urlencode

from app.api.deps import get_db, get_current_user
from app.config import settings
from app.middleware.rate_limit import login_rate_limit, sso_callback_rate_limit
from app.models.user import User
from app.schemas.auth import (
    UserLogin, ChangePassword, TokenResponse, UserInfo,
    RefreshRequest,
)
from app.schemas.common import MessageResponse
from app.services import audit_service, auth_service, sso_service

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


@router.post("/login", response_model=TokenResponse)
async def login(
    data: UserLogin,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(login_rate_limit),
):
    """用户登录（同一 IP 每分钟最多 settings.login_rate_limit_per_min 次（默认 5；批次F 验收恢复，批次5.5 压测曾提 30），防弱口令爆破）"""
    token = await auth_service.login_user(db, data.username, data.password)
    # 审计：登录成功留痕（失败登录不记录——避免审计被爆破日志刷爆）
    from sqlalchemy import select
    result = await db.execute(select(User).where(User.username == data.username))
    user = result.scalar_one_or_none()
    if user:
        await audit_service.record_audit(db, user.id, user.username, "user.login", request=request)
    return token


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """刷新 Access Token（refresh token 放请求体，避免进入访问日志）"""
    return await auth_service.refresh_access_token(data.refresh_token, db)


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    data: ChangePassword,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """修改密码（首登强制改密的员工在此清除标记）"""
    await auth_service.change_password(
        db, current_user, data.old_password, data.new_password
    )
    await audit_service.record_audit(db, current_user.id, current_user.username, "user.change_password", request=request
    )
    return MessageResponse(message="密码修改成功")


@router.get("/me", response_model=UserInfo)
async def get_me(current_user: User = Depends(get_current_user)):
    """获取当前用户信息"""
    return auth_service.get_user_info(current_user)


# ─────────────────────────── SSO 登录插槽（阶段4） ───────────────────────────

@router.get("/sso/providers")
async def sso_providers():
    """已启用的 SSO 登录方式列表（前端据此显示扫码按钮；未配置的 provider 不出现）"""
    return {"providers": [{"name": p.name} for p in sso_service.get_enabled_providers()]}


@router.get("/sso/{provider}/url")
async def sso_auth_url(provider: str):
    """生成授权链接（state 已签名防 CSRF，10 分钟有效）"""
    p = sso_service.get_provider(provider)
    state = sso_service._sign_state(provider)
    return {"auth_url": p.build_auth_url(state)}


@router.get("/sso/{provider}/callback")
async def sso_callback(
    provider: str,
    code: str,
    state: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(sso_callback_rate_limit),
):
    """SSO 回调：code 换身份 → 绑定登录或自动建号 → 302 回前端。

    token 放 URL fragment（# 后）——fragment 不会进 nginx/浏览器历史访问日志，
    比 query 参数更安全。回调按 IP 限频（无认证门槛的端点，防刷回调换身份）。
    """
    p = sso_service.get_provider(provider)
    sso_service._verify_state(provider, state)
    info = await p.exchange_code(code)
    user = await sso_service.login_or_create_user(db, provider, info)
    tokens = await sso_service.complete_login(db, user)
    await audit_service.record_audit(db, user.id, user.username, "user.sso_login",
        detail={"provider": provider}, request=request,
    )
    fragment = urlencode({
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
    })
    redirect_to = f"{settings.frontend_base_url}/sso-callback#{fragment}"
    return RedirectResponse(redirect_to, status_code=302)
