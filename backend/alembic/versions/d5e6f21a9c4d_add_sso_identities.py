"""add user_sso_identities

Revision ID: d5e6f21a9c4d
Revises: c9d4e21f8a3b
Create Date: 2026-09-24

企业知识库改造 阶段4：SSO 登录插槽。
- 新表 user_sso_identities：第三方身份源账号与本地账号的绑定
  （provider/openid 唯一——同一外部身份不会重复建号）
"""
from alembic import op
import sqlalchemy as sa


revision = "d5e6f21a9c4d"
down_revision = "c9d4e21f8a3b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_sso_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("openid", sa.String(128), nullable=False),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("department_names", sa.JSON(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "openid", name="uq_sso_provider_openid"),
    )
    op.create_index("ix_user_sso_identities_user_id", "user_sso_identities", ["user_id"])
    op.create_index("ix_user_sso_identities_provider", "user_sso_identities", ["provider"])


def downgrade() -> None:
    op.drop_index("ix_user_sso_identities_provider", table_name="user_sso_identities")
    op.drop_index("ix_user_sso_identities_user_id", table_name="user_sso_identities")
    op.drop_table("user_sso_identities")
