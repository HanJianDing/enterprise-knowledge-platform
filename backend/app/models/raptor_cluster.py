"""RAPTOR 摘要树簇关系表（批次11a）——摘要块与其子树叶子块的映射

摘要块 metadata 只能放标量（Chroma metadata 不支持列表），簇→子块关系
存本表 JSON 列。检索命中摘要块后按 (document_id, chunk_index) 展开子树
叶子追加上下文——折叠树模式（RAPTOR 论文 collapsed-tree）。
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base


class RaptorCluster(Base):
    """摘要树一个簇：tree_level 层 cluster_id 簇的摘要块与子块索引列表。

    child_chunk_indices 为本层簇的成员块在该文档内的 chunk_index 列表
    （层 1 的成员是叶子块；层 2+ 的成员是下一层摘要块的 chunk_index——
    递归展开到叶子为止）。
    """

    __tablename__ = "raptor_clusters"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    tree_level: Mapped[int] = mapped_column(Integer, nullable=False)  # 1=叶子层之上
    cluster_id: Mapped[str] = mapped_column(String(36), nullable=False)
    summary_chunk_index: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # 摘要块在 Chroma 内的 chunk_index（摘要块独立编号序列）
    child_chunk_indices: Mapped[list[int]] = mapped_column(
        JSON, nullable=False, default=list
    )  # 本簇成员块的 chunk_index 列表
    built_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
