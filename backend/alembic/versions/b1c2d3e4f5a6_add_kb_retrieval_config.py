"""add knowledge_bases retrieval_config

Revision ID: b1c2d3e4f5a6
Revises: a5c6d7e8f9a0
Create Date: 2026-10-04

验收批次1·1d：知识库级检索参数（JSON），库级覆盖全局默认
（top_k / pagerank_alpha），空=全局默认；管理页"检索配置"维护。
"""
from alembic import op
import sqlalchemy as sa

revision = "b1c2d3e4f5a6"
down_revision = "a5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_bases", sa.Column("retrieval_config", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_bases", "retrieval_config")
