"""add mcp_credentials

Revision ID: f0a1b23c4d5e
Revises: e9c0d12e4f5a
Create Date: 2026-09-27

批次D：外部接入凭证表——长期 MCP 接入凭证（绑定账号+可见库白名单+可撤销），
原文哈希存储，创建时一次性展示。
"""
from alembic import op
import sqlalchemy as sa


revision = "f0a1b23c4d5e"
down_revision = "e9c0d12e4f5a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(200), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("kb_ids", sa.JSON(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_mcp_credentials_owner", "mcp_credentials", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_credentials_owner", table_name="mcp_credentials")
    op.drop_table("mcp_credentials")
