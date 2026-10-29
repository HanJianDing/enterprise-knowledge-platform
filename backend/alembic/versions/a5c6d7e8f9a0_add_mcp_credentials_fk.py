"""add mcp_credentials owner_user_id foreign key

Revision ID: a5c6d7e8f9a0
Revises: f0a1b23c4d5e
Create Date: 2026-09-28

验收批次E（质检 D 批遗留 S2 低危）：模型声明了 ForeignKey("users.id")，
但建表迁移（f0a1b23c4d5e）漏建约束——孤儿凭证理论上可入库（验证时
fail-closed 拒绝、无提权风险，故仅低危）。补建外键约束与模型声明对齐。
"""
from alembic import op

revision = "a5c6d7e8f9a0"
down_revision = "f0a1b23c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_mcp_credentials_owner_user_id",
        "mcp_credentials",
        "users",
        ["owner_user_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_mcp_credentials_owner_user_id",
        "mcp_credentials",
        type_="foreignkey",
    )
