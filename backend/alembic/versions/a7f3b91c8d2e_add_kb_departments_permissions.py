"""add knowledge_bases / departments / permissions

Revision ID: a7f3b91c8d2e
Revises: c4d7a91e2b5f
Create Date: 2026-09-23

企业知识库改造 阶段1：
- 新表：knowledge_bases（知识库）、departments（部门树）、user_departments（人-部门）、
  kb_members（库授权：部门/个人 × 读/传/管）
- documents 与 knowledge_categories 加 kb_id 归属列（NOT NULL）
- 存量数据回填：已有文档/分类时自动创建「默认知识库」（is_public=True，延续改造前全员共享行为）
- 分类名唯一约束从"全局唯一"改为"同库内唯一"（不同库可以有同名分类）

SQLite 分支（仅本地开发库/测试可能遇到）：只做建表+加列+回填，不强制 NOT NULL、
不做约束替换（SQLite 需要整表重建，且测试环境用 create_all 不受迁移影响）。
生产（PostgreSQL）走完整路径。
"""
import uuid

from alembic import op
import sqlalchemy as sa


revision = "a7f3b91c8d2e"
down_revision = "c4d7a91e2b5f"
branch_labels = None
depends_on = None

DEFAULT_KB_NAME = "默认知识库"


def _create_default_kb_if_needed() -> str | None:
    """存量数据存在时创建默认知识库并返回其 ID；无存量数据返回 None（启动种子会补建）"""
    bind = op.get_bind()
    has_docs = bind.execute(sa.text("SELECT COUNT(*) FROM documents")).scalar() > 0
    has_cats = bind.execute(sa.text("SELECT COUNT(*) FROM knowledge_categories")).scalar() > 0
    if not (has_docs or has_cats):
        return None

    admin_id = bind.execute(
        sa.text("SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1")
    ).scalar()
    if admin_id is None:
        admin_id = bind.execute(sa.text("SELECT id FROM users LIMIT 1")).scalar()
    if admin_id is None:
        return None  # 理论不可达：documents.uploaded_by 必挂用户

    kb_id = str(uuid.uuid4())
    bind.execute(
        sa.text(
            "INSERT INTO knowledge_bases "
            "(id, name, description, is_public, created_by, created_at, updated_at) "
            "VALUES (:id, :name, :desc, true, :created_by, now(), now())"
        ).bindparams(
            id=kb_id, name=DEFAULT_KB_NAME,
            desc="迁移自动创建的默认知识库（全员可读），可重命名或设为私有后按需授权",
            created_by=admin_id,
        )
    )
    return kb_id


def upgrade() -> None:
    # ── 新表 ──
    op.create_table(
        "departments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_table(
        "user_departments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "department_id", name="uq_user_department"),
    )
    op.create_index("ix_user_departments_user_id", "user_departments", ["user_id"])
    op.create_index("ix_user_departments_department_id", "user_departments", ["department_id"])

    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_table(
        "kb_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kb_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("can_read", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("can_upload", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_manage", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("kb_id", "subject_type", "subject_id", name="uq_kb_member_subject"),
    )
    op.create_index("ix_kb_members_kb_id", "kb_members", ["kb_id"])
    op.create_index("ix_kb_members_subject_id", "kb_members", ["subject_id"])

    # ── 存量回填：默认知识库 ──
    default_kb_id = _create_default_kb_if_needed()

    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        # ── documents.kb_id ──
        op.add_column("documents", sa.Column("kb_id", sa.String(36), nullable=True))
        if default_kb_id:
            op.execute(
                sa.text("UPDATE documents SET kb_id = :kb WHERE kb_id IS NULL")
                .bindparams(kb=default_kb_id)
            )
        op.alter_column("documents", "kb_id", nullable=False)
        op.create_foreign_key("fk_documents_kb_id", "documents", "knowledge_bases", ["kb_id"], ["id"])
        op.create_index("ix_documents_kb_id", "documents", ["kb_id"])

        # ── knowledge_categories.kb_id ──
        op.add_column("knowledge_categories", sa.Column("kb_id", sa.String(36), nullable=True))
        if default_kb_id:
            op.execute(
                sa.text("UPDATE knowledge_categories SET kb_id = :kb WHERE kb_id IS NULL")
                .bindparams(kb=default_kb_id)
            )
        op.alter_column("knowledge_categories", "kb_id", nullable=False)
        op.create_foreign_key(
            "fk_knowledge_categories_kb_id", "knowledge_categories", "knowledge_bases", ["kb_id"], ["id"]
        )
        op.create_index("ix_knowledge_categories_kb_id", "knowledge_categories", ["kb_id"])

        # ── 分类名唯一：全局唯一 → 同库内唯一 ──
        op.drop_constraint("knowledge_categories_name_key", "knowledge_categories", type_="unique")
        op.create_unique_constraint("uq_kb_category_name", "knowledge_categories", ["kb_id", "name"])
    else:
        # SQLite：只加列 + 回填（NOT NULL/约束替换需要整表重建，开发库不必为此付出）
        op.add_column("documents", sa.Column("kb_id", sa.String(36), nullable=True))
        op.add_column("knowledge_categories", sa.Column("kb_id", sa.String(36), nullable=True))
        if default_kb_id:
            op.execute(
                sa.text("UPDATE documents SET kb_id = :kb WHERE kb_id IS NULL")
                .bindparams(kb=default_kb_id)
            )
            op.execute(
                sa.text("UPDATE knowledge_categories SET kb_id = :kb WHERE kb_id IS NULL")
                .bindparams(kb=default_kb_id)
            )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.drop_constraint("uq_kb_category_name", "knowledge_categories", type_="unique")
        op.create_unique_constraint("knowledge_categories_name_key", "knowledge_categories", ["name"])
        op.drop_index("ix_knowledge_categories_kb_id", table_name="knowledge_categories")
        op.drop_constraint("fk_knowledge_categories_kb_id", "knowledge_categories", type_="foreignkey")
        op.drop_column("knowledge_categories", "kb_id")
        op.drop_index("ix_documents_kb_id", table_name="documents")
        op.drop_constraint("fk_documents_kb_id", "documents", type_="foreignkey")
        op.drop_column("documents", "kb_id")
    else:
        op.drop_column("knowledge_categories", "kb_id")
        op.drop_column("documents", "kb_id")

    op.drop_index("ix_kb_members_subject_id", table_name="kb_members")
    op.drop_index("ix_kb_members_kb_id", table_name="kb_members")
    op.drop_table("kb_members")
    op.drop_table("knowledge_bases")
    op.drop_index("ix_user_departments_department_id", table_name="user_departments")
    op.drop_index("ix_user_departments_user_id", table_name="user_departments")
    op.drop_table("user_departments")
    op.drop_table("departments")
