"""add message_feedbacks

Revision ID: f2a8b41c9d3e
Revises: 4d1e8c2a9f60
Create Date: 2026-09-03

反馈系统：用户对 AI 回答的点赞/点踩（点踩可带意见）。
- message_id / user_id 双外键（级联删除）+ 联合唯一约束（防并发双击两条）
- 索引：message_id、user_id 各一个（评价查询与"我评价过哪些"查询用）
"""
from alembic import op
import sqlalchemy as sa


revision = "f2a8b41c9d3e"
down_revision = "4d1e8c2a9f60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_feedbacks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.String(10), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", "user_id", name="uq_feedback_message_user"),
    )
    op.create_index("ix_message_feedbacks_message_id", "message_feedbacks", ["message_id"])
    op.create_index("ix_message_feedbacks_user_id", "message_feedbacks", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_message_feedbacks_user_id", table_name="message_feedbacks")
    op.drop_index("ix_message_feedbacks_message_id", table_name="message_feedbacks")
    op.drop_table("message_feedbacks")
