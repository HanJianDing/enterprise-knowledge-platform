"""add approval_rules

Revision ID: a7c9d2e4f6b8
Revises: b1c2d3e4f5a6
Create Date: 2026-10-08

批次2-4 规则腿：审批链规则表——审批类文档解析产物（纯规则解析，
不烧 LLM），供"报销 3000 元谁审批"类问题确定性匹配。
- business_type：报销/采购/请假
- min_value/max_value：区间（统一换算单位存储；max 可空=上不封顶）
- approver：审批人/机构文本（原文）
- action：审批/备案
- source_doc_id/source_text：证据溯源（回答可标出处）
"""
from alembic import op
import sqlalchemy as sa


revision = "a7c9d2e4f6b8"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_type", sa.String(20), nullable=False, index=True),
        sa.Column("min_value", sa.Float(), nullable=True),
        sa.Column("max_value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(10), nullable=False, server_default="元"),
        sa.Column("approver", sa.String(50), nullable=False),
        sa.Column("action", sa.String(10), nullable=False, server_default="审批"),
        sa.Column("source_doc_id", sa.String(36), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("approval_rules")
