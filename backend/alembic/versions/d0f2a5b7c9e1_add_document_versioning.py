"""add document versioning

Revision ID: d0f2a5b7c9e1
Revises: c9e1f4a6b8d0
Create Date: 2026-10-09

批次5 7b：文件版本管理——重传同名文档不覆盖旧版（旧版标 superseded 保留
数据），新文档 version_of 指向上一个版本、version_no 递增。
- version_of：上一个版本 doc_id（None=首版）
- version_no：版本号（1 起递增）
"""
from alembic import op
import sqlalchemy as sa


revision = "d0f2a5b7c9e1"
down_revision = "c9e1f4a6b8d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents",
                  sa.Column("version_of", sa.String(36), nullable=True))
    # 质检批次5 低#5：down 反查（version_of 指向当前文档）需要索引
    op.create_index("ix_documents_version_of", "documents", ["version_of"])
    op.add_column("documents",
                  sa.Column("version_no", sa.Integer(), nullable=False,
                            server_default="1"))


def downgrade() -> None:
    op.drop_index("ix_documents_version_of", table_name="documents")
    op.drop_column("documents", "version_no")
    op.drop_column("documents", "version_of")
