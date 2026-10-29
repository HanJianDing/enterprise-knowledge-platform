"""知识库/部门/权限相关 Schema（企业知识库改造 阶段1）"""
from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    is_public: bool = False


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    is_public: bool | None = None


class KnowledgeBaseResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    is_public: bool
    created_by: str
    created_at: datetime
    document_count: int = 0
    member_count: int = 0

    model_config = {"from_attributes": True}


class DepartmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    parent_id: str | None = None


class DepartmentUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    parent_id: str | None = None


class DepartmentResponse(BaseModel):
    id: str
    name: str
    parent_id: str | None = None
    children: list["DepartmentResponse"] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class KbMemberIn(BaseModel):
    """一条授权：授权给部门或给个人"""
    subject_type: str = Field(..., pattern="^(dept|user)$")
    subject_id: str
    can_read: bool = True
    can_upload: bool = False
    can_manage: bool = False

    @model_validator(mode="after")
    def _normalize_read(self) -> "KbMemberIn":
        # 能传/能管的人必然要能读：can_read 强制补真，前端勾选"传/管"时读也自动带上
        if self.can_upload or self.can_manage:
            self.can_read = True
        return self


class KbMembersUpdate(BaseModel):
    """整体替换授权列表（PUT 语义：以提交的列表为准，旧授权删除）"""
    members: list[KbMemberIn] = []


class KbMemberOut(BaseModel):
    id: str
    subject_type: str
    subject_id: str
    subject_name: str  # 部门名或用户名（前端展示）
    can_read: bool
    can_upload: bool
    can_manage: bool
    created_at: datetime


class UserDepartmentsUpdate(BaseModel):
    """设置用户所属部门（整体替换）"""
    department_ids: list[str] = []
