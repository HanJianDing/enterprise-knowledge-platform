"""add rule effective_date

Revision ID: b8d0e3f5a7c9
Revises: a7c9d2e4f6b8
Create Date: 2026-10-08

批次3 冲突裁决语义修复：approval_rules 加 effective_date（制度生效日期，
"自 X 年 X 月 X 日起生效"解析）——裁决依据从"上传时间"改为"版本日期"：
旧制度后归档（上传晚）不应赢过现行制度。
"""
from alembic import op
import sqlalchemy as sa


revision = "b8d0e3f5a7c9"
down_revision = "a7c9d2e4f6b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approval_rules",
                  sa.Column("effective_date", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("approval_rules", "effective_date")
