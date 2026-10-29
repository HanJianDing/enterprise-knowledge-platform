"""add document process_log

Revision ID: e7a8b12c3d4e
Revises: d5e6f21a9c4d
Create Date: 2026-09-24

企业知识库产品化 A3：文档解析事件流水。
- documents.process_log（JSON，可空）：[{"time","stage","detail"}]，
  前端"解析流水"弹窗按时间轴展示（上传/开始处理/完成/失败/重试入队）
"""
from alembic import op
import sqlalchemy as sa


revision = "e7a8b12c3d4e"
down_revision = "d5e6f21a9c4d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("process_log", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "process_log")
