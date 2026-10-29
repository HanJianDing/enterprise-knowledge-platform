"""add api_keys

Revision ID: e1a3b6c8d9f0
Revises: d0f2a5b7c9e1
Create Date: 2026-10-10

批次11e OpenAI 兼容 API：API key 表——通用 LLM 客户端（Dify/n8n/
Coze/LangChain）以 Bearer key 调用 /v1/chat/completions。
- key_hash：SHA-256 哈希存储（明文 key 只在生成时返回一次）
- 关联用户（key 的可见库范围=该用户的可见库，权限穿透）
- 支持吊销（is_active）与过期时间
"""
from alembic import op
import sqlalchemy as sa


revision = "e1a3b6c8d9f0"
down_revision = "d0f2a5b7c9e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(20), nullable=False),  # 展示用（sk-rag-+8位hex）
        sa.Column("name", sa.String(100), nullable=False),       # 用途备注
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_api_keys_user", "api_keys", ["user_id"])


def downgrade() -> None:
    op.drop_table("api_keys")
