"""认证相关 Schema"""
import re

from pydantic import BaseModel, Field, field_validator

# 密码策略（产品化 C2）：至少 8 位且同时包含字母与数字。
# 登录不校验（存量弱密码账号不应被锁死），只在设置密码处生效（建号/改密/重置/CSV 导入）。
# 用 field_validator 而非 Field(pattern=...)：Python re 不支持 lookahead，
# "同时含字母和数字"无法用单一正则表达。
PASSWORD_RULE_HINT = "密码需至少 8 位且同时包含字母和数字"


def validate_password_strength(v: str) -> str:
    """密码强度校验（建号/改密/重置/CSV 导入共用）"""
    if len(v) < 8 or not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
        raise ValueError(PASSWORD_RULE_HINT)
    return v


class UserLogin(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=6, max_length=50)


class ChangePassword(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8, max_length=50)

    @field_validator("new_password")
    @classmethod
    def _check_strength(cls, v: str) -> str:
        return validate_password_strength(v)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    # 首登强制改密标记（管理员建号/批量导入的账号为 True，前端据此跳转改密页）
    must_change_password: bool = False


class RefreshRequest(BaseModel):
    """刷新 Access Token 的请求体。

    安全修复：refresh token 不再走 URL 查询参数（会落进 nginx/代理访问日志）。
    """
    refresh_token: str = Field(..., min_length=10)


class UserInfo(BaseModel):
    id: str
    username: str
    email: str
    role: str
    is_active: bool
    must_change_password: bool = False

    model_config = {"from_attributes": True}


# ── 管理员建号/批量导入（企业知识库改造阶段3：开放注册已关闭） ──

class UserCreate(BaseModel):
    """管理员创建单个员工账号（初始密码首登强制修改）"""
    username: str = Field(..., min_length=2, max_length=50)
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=8, max_length=50)
    role: str = Field(default="user", pattern="^(admin|editor|user|auditor)$")
    department_ids: list[str] = []

    @field_validator("password")
    @classmethod
    def _check_strength(cls, v: str) -> str:
        return validate_password_strength(v)


class ResetPasswordRequest(BaseModel):
    """管理员重置员工密码（产品化 C2：重置后首登强制改密）"""
    new_password: str = Field(..., min_length=8, max_length=50)

    @field_validator("new_password")
    @classmethod
    def _check_strength(cls, v: str) -> str:
        return validate_password_strength(v)


class UserImportRowError(BaseModel):
    row: int
    username: str
    reason: str


class UserImportResponse(BaseModel):
    created: int
    errors: list[UserImportRowError] = []
