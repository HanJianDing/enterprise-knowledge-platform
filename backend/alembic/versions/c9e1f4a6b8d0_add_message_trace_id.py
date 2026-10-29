"""add message trace_id

Revision ID: c9e1f4a6b8d0
Revises: b8d0e3f5a7c9
Create Date: 2026-10-09

批次4 可观测（质检 N2）：messages 加 trace_id 列——trace 链接在
主路径刷新会话后仍可见（此前 trace_id 仅存在于 SSE done 事件、
不持久化，前端回退路径构造的消息有值、主路径 getConversation 丢失）。
"""
from alembic import op
import sqlalchemy as sa


revision = "c9e1f4a6b8d0"
down_revision = "b8d0e3f5a7c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("trace_id", sa.String(40), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "trace_id")
