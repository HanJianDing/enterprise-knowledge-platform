"""认证服务"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException, status

from app.models.user import User
from app.middleware.auth import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.schemas.auth import TokenResponse, UserInfo

# 合法角色（企业知识库改造阶段3：admin | editor | user；开放注册已关闭）
ALLOWED_ROLES = ("admin", "editor", "user")


async def create_user(
    db: AsyncSession,
    username: str,
    email: str,
    password: str,
    role: str = "user",
    must_change_password: bool = True,
) -> User:
    """创建账号（管理员建号/批量导入用；初始密码首登强制修改）。

    企业知识库改造阶段3：原开放注册接口已移除，建号只走管理端。
    """
    if role not in ALLOWED_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"非法角色: {role}，允许: {', '.join(ALLOWED_ROLES)}",
        )

    # bcrypt 上限 72 字节（中文 1 字=3 字节）：超长密码会 500，创建前友好拦截
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="密码过长（最多 24 个中文字符）",
        )

    # 检查用户名唯一性
    result = await db.execute(select(User).where(User.username == username))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名已存在",
        )

    # 检查邮箱唯一性
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="邮箱已被使用",
        )

    user = User(
        username=username,
        email=email,
        hashed_password=hash_password(password),
        role=role,
        must_change_password=must_change_password,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


async def login_user(
    db: AsyncSession,
    username: str,
    password: str,
) -> TokenResponse:
    """用户登录，返回双 Token"""
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已被禁用",
        )

    # 更新最后登录时间 + 顶号版本号。
    # 原子自增（数据库端 session_version = session_version + 1）：
    # 两台设备并发登录时各自拿到不同的版本，先登录的一侧必被踢，
    # 避免 Python 读-改-写竞态导致"两票同版本、谁都踢不掉"。
    from datetime import datetime, timezone
    from sqlalchemy import update as sa_update
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await db.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(last_login_at=now, session_version=User.session_version + 1)
        .returning(User.session_version)
    )
    user.session_version = result.scalar_one()
    user.last_login_at = now

    return TokenResponse(
        access_token=create_access_token(user.id, user.role, user.session_version),
        refresh_token=create_refresh_token(user.id, user.session_version),
        must_change_password=user.must_change_password,
    )


async def change_password(
    db: AsyncSession,
    user: User,
    old_password: str,
    new_password: str,
) -> None:
    """修改密码。

    上线安全修复：改密后 session_version += 1，所有已签发的
    access/refresh 令牌立即失效（sv 校验不通过），全设备强制重新登录——
    防"密码被盗后改密，坏人仍保持登录最多 7 天"。
    企业知识库改造阶段3：改密成功即清除首登强制改密标记。
    """
    if not verify_password(old_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="旧密码不正确",
        )

    if len(new_password.encode("utf-8")) > 72:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="新密码过长（最多 24 个中文字符）",
        )

    user.hashed_password = hash_password(new_password)
    user.session_version += 1  # 踢掉所有旧令牌
    user.must_change_password = False  # 首登改密完成
    await db.flush()


async def refresh_access_token(refresh_token: str, db: AsyncSession) -> TokenResponse:
    """刷新 Access Token"""
    payload = decode_token(refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请使用 Refresh Token",
        )

    user_id = payload.get("sub")
    # 从数据库获取最新角色
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已禁用",
        )
    # 顶号校验：refresh 令牌的 sv 与当前会话版本不一致（期间他处登录过）→ 拒绝换票
    if payload.get("sv") != user.session_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号已在其他设备登录，请重新登录",
        )
    return TokenResponse(
        access_token=create_access_token(user_id, user.role, user.session_version),
        refresh_token=refresh_token,
        must_change_password=user.must_change_password,
    )


def get_user_info(user: User) -> UserInfo:
    """获取用户信息"""
    return UserInfo.model_validate(user)
