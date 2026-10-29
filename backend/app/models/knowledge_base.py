"""知识库、部门与权限模型（企业知识库改造 阶段1）

权限模型：部门-库绑定 + 个人覆盖（不做完整 RBAC 三表）。
判定规则一句话：个人授权 > 部门授权 > 默认无权限；admin 在代码层短路恒有全部权限。
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    String, Boolean, DateTime, ForeignKey, JSON, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base


class Department(Base):
    """部门（组织树，parent_id 自引用；权限继承不做——授权只认直接绑定）"""
    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("departments.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    children: Mapped[list["Department"]] = relationship(
        "Department", backref="parent", remote_side="Department.id"
    )


class UserDepartment(Base):
    """用户-部门绑定（SSO 阶段 4 会按企微/钉钉组织架构同步写入）"""
    __tablename__ = "user_departments"
    __table_args__ = (
        UniqueConstraint("user_id", "department_id", name="uq_user_department"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    department_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("departments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class KnowledgeBase(Base):
    """知识库（多库隔离；is_public=True 时全员可读，如公司公共制度库）"""
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    # 检索参数（验收批次1·1d）：库级覆盖全局默认，{"top_k":6,"pagerank_alpha":0.0}，
    # 空对象=全用全局默认。调优改配置不改代码，管理页"检索配置"表单维护
    retrieval_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    documents: Mapped[list["Document"]] = relationship(back_populates="kb")
    categories: Mapped[list["KnowledgeCategory"]] = relationship(back_populates="kb")
    members: Mapped[list["KbMember"]] = relationship(
        back_populates="kb", cascade="all, delete-orphan"
    )


class KbMember(Base):
    """知识库授权：subject_type=dept（整部门授权）| user（个人覆盖）"""
    __tablename__ = "kb_members"
    __table_args__ = (
        UniqueConstraint("kb_id", "subject_type", "subject_id", name="uq_kb_member_subject"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)  # dept | user
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    can_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_upload: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_manage: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    kb: Mapped["KnowledgeBase"] = relationship(back_populates="members")
