"""add users.session_version

Revision ID: 4d1e8c2a9f60
Revises: b03b483c5c4d
Create Date: 2026-08-27

顶号机制：users 表新增 session_version 列（每次登录 +1，令牌携带 sv，
与当前值不一致判定"已在其他设备登录"）。
"""
from alembic import op
import sqlalchemy as sa


revision = "4d1e8c2a9f60"
down_revision = "b03b483c5c4d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "session_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "session_version")
