"""add raptor_clusters

Revision ID: f1b2c3d4e5a6
Revises: e1a3b6c8d9f0
Create Date: 2026-10-10

批次11a RAPTOR 摘要树：簇关系表——摘要块（Chroma）与子树叶子块的映射。
命中摘要块后按 (document_id, chunk_index) 展开子树叶子追加上下文
（折叠树模式，论文 collapsed-tree）。
"""
from alembic import op
import sqlalchemy as sa


revision = "f1b2c3d4e5a6"
down_revision = "e1a3b6c8d9f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "raptor_clusters",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("tree_level", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("summary_chunk_index", sa.Integer(), nullable=False),
        sa.Column("child_chunk_indices", sa.JSON(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        # 质检 P2-①：模型声明了 FK，迁移必须建（schema 漂移修复）；
        # CASCADE——文档删除时簇行由 DB 级联清理（delete_tree_for_document
        # 的显式清理保留为幂等兜底）
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_raptor_clusters_document", "raptor_clusters",
                    ["document_id"])


def downgrade() -> None:
    op.drop_table("raptor_clusters")
