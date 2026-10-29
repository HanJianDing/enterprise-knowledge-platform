"""remove agent / ticket tables

Revision ID: b2e8f47c1d9a
Revises: a7f3b91c8d2e
Create Date: 2026-09-23

企业知识库改造 阶段2：客服功能彻底剥离。
- users.role='agent' 全部降为 'user'（角色枚举收窄，editor 由阶段3加入）
- 删除 tickets / agent_presence / agent_shifts 三张表
- 清理 system_settings 中的全局默认工时键

前置要求（方案约定）：执行本迁移前必须先跑 scripts/archive_tickets.py
归档三表数据 + pg_dump 全库 + Chroma 目录 tar 双备份。
downgrade 只能重建空表结构，数据以归档 JSON 为准（不回灌线上）。
"""
from alembic import op
import sqlalchemy as sa


revision = "b2e8f47c1d9a"
down_revision = "a7f3b91c8d2e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 客服账号降为普通员工（历史人工回复消息保留，那是会话历史的一部分）
    op.execute(sa.text("UPDATE users SET role = 'user' WHERE role = 'agent'"))

    op.execute(sa.text(
        "DELETE FROM system_settings "
        "WHERE key IN ('agent_work_start_hour', 'agent_work_end_hour')"
    ))

    op.drop_table("tickets")
    op.drop_table("agent_presence")
    op.drop_table("agent_shifts")


def downgrade() -> None:
    # 只重建空表结构（数据不回灌，以归档 JSON 为准）
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("summary", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("assigned_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_tickets_conversation_id", "tickets", ["conversation_id"])
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"])
    op.create_index("ix_tickets_agent_id", "tickets", ["agent_id"])

    op.create_table(
        "agent_presence",
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "agent_shifts",
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("start_hour", sa.Integer(), nullable=False),
        sa.Column("end_hour", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
