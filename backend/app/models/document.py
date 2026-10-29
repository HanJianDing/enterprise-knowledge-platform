"""文档与知识分类模型"""
import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, ForeignKey, Text, BigInteger, JSON, func, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base


class KnowledgeCategory(Base):
    __tablename__ = "knowledge_categories"
    # 阶段1（企业知识库）：分类归属知识库，名称在同一库内唯一
    # （旧全局唯一约束已由迁移替换——不同库可以有同名分类，如各自建"报销"）
    # 验收批次B：唯一约束加入 parent_id——两层分类树下不同主题父分类
    # 可以有同名形态子分类（如"考勤-制度"与"假期-制度"各自成立）
    __table_args__ = (
        UniqueConstraint("kb_id", "parent_id", "name", name="uq_kb_category_parent_name"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("knowledge_categories.id"), nullable=True)
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # 关联
    # 批次F 验收修复：自引用 backref="parent" 被推断为集合（uselist=True，
    # 反向解析出 ONETOMANY）——显式 back_populates 双向定义，parent 是标量
    # （child.parent → 父分类），children 是集合（parent.children → 子分类）；
    # remote_side 只写在 many-to-one 侧（双向关系共享连接条件，
    # 两侧都写会方向冲突 ArgumentError）
    parent: Mapped["KnowledgeCategory | None"] = relationship(
        "KnowledgeCategory", back_populates="children",
        remote_side="KnowledgeCategory.id",
    )
    children: Mapped[list["KnowledgeCategory"]] = relationship(
        "KnowledgeCategory", back_populates="parent",
    )
    documents: Mapped[list["Document"]] = relationship(back_populates="category")
    kb: Mapped["KnowledgeBase"] = relationship(back_populates="categories")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    original_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)  # pdf, txt, md, docx, csv, xlsx
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )
    category_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("knowledge_categories.id"), nullable=True)
    content_type: Mapped[str] = mapped_column(String(20), nullable=False, default="long_text")  # faq | structured | long_text | chat_log
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploaded")  # uploaded→processing→completed|failed|superseded(批次5 被新版替换)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 解析事件流水（产品化 A3）：[{"time", "stage", "detail"}]，随处理进程追加，
    # 前端"解析流水"弹窗按时间轴展示（上传/开始/完成/失败/重试入队）
    process_log: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    # 批次5 7b 版本管理：重传同名文档不覆盖——旧版标 superseded 保留数据，
    # 新文档 version_of 指向上一个版本、version_no 递增（None/1=首版）
    version_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # 关联
    category: Mapped["KnowledgeCategory | None"] = relationship(back_populates="documents")
    uploader: Mapped["User"] = relationship(back_populates="documents")
    kb: Mapped["KnowledgeBase"] = relationship(back_populates="documents")
