"""batch F backlog #12/#13: api_keys.kb_ids + mcp_servers table

Revision ID: f3c4d5e6f7a8
Revises: f1b2c3d4e5a6
Create Date: 2026-10-16

批次F 挂清单攒批：
- #12 API key 库白名单：api_keys 加 kb_ids（JSON，null=穿透账号权限）
- #13 MCP 工具管理页：mcp_servers 配置表（页面增删改查落库）
"""
from alembic import op
import sqlalchemy as sa


revision = "f3c4d5e6f7a8"
down_revision = "f1b2c3d4e5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("kb_ids", sa.JSON(), nullable=True))
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("transport", sa.String(10), nullable=False),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("command", sa.String(200), nullable=True),
        sa.Column("args_json", sa.String(1000), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("mcp_servers")
    op.drop_column("api_keys", "kb_ids")
