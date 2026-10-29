"""add created_at indexes for stats

Revision ID: c4d7a91e2b5f
Revises: f2a8b41c9d3e
Create Date: 2026-09-03

图表看板趋势查询（按 created_at 过滤 + 按天分组）直接受益的索引：
conversations / messages / message_feedbacks 各一个。
"""
from alembic import op


revision = "c4d7a91e2b5f"
down_revision = "f2a8b41c9d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_conversations_created_at", "conversations", ["created_at"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])
    op.create_index("ix_message_feedbacks_created_at", "message_feedbacks", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_message_feedbacks_created_at", table_name="message_feedbacks")
    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_index("ix_conversations_created_at", table_name="conversations")
