"""JWT 认证中间件"""
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.infrastructure.database import get_db
from app.models.user import User

# JWT Bearer。auto_error=False：缺失凭证由 get_current_user 手动抛 401——
# HTTPBearer 默认对缺失凭证抛 403，且该行为随 fastapi/starlette 版本变化
# （本地升级到 fastapi 0.115/starlette 0.38 后 6 个认证测试从 401 变 403），
# 手动处理保证"未认证=401"的 API 契约与依赖版本无关
security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """密码哈希"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码。

    超长密码（>72 字节）触发 bcrypt 上限抛 ValueError：按"不匹配"处理，
    防登录/改密接口 500（注册侧另有字节长度友好拦截）。
    """
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"), hashed_password.encode("utf-8")
        )
    except ValueError:
        return False


def create_access_token(user_id: str, role: str, session_version: int = 0) -> str:
    """创建 Access Token (短期)。sv 声明用于顶号校验；默认 0 会被校验拒绝
    （fail-closed），真实调用方必须传入用户当前的 session_version。"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    to_encode = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "sv": session_version,
        "exp": expire,
    }
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str, session_version: int = 0) -> str:
    """创建 Refresh Token (长期)。携带 sv：刷新时校验，防被顶设备拿旧 refresh 换新票。"""
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    to_encode = {
        "sub": user_id,
        "type": "refresh",
        "sv": session_version,
        "exp": expire,
    }
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """解码并验证 JWT Token"""
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭证",
        )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从 JWT 获取当前用户"""
    if credentials is None:
        # 未携带凭证：401（不依赖 HTTPBearer 的默认行为，见 security 定义注释）
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证凭证",
        )
    payload = decode_token(credentials.credentials)

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请使用 Access Token",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 信息不完整",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已被禁用",
        )

    # 顶号校验：令牌携带的会话版本与用户当前版本不一致 → 已在其他设备登录。
    # 缺省参数 0 意味着"任何真实令牌都必须携带 sv"（fail-closed，防旧令牌绕过）。
    sv = payload.get("sv")
    if sv is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录凭证已过期，请重新登录",
        )
    if sv != user.session_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号已在其他设备登录，请重新登录",
        )

    return user


async def require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """要求管理员权限"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


async def require_audit_view(
    current_user: User = Depends(get_current_user),
) -> User:
    """要求审计查看权限：admin 或 auditor（产品化 C5——审计员只读审计，
    无任何其他管理权限；能看审计的人与能改权限的人分离，治理分层）"""
    if current_user.role not in ("admin", "auditor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员或审计员可访问",
        )
    return current_user
