"""category uniqueness includes parent_id

Revision ID: e9c0d12e4f5a
Revises: e7a8b12c3d4e
Create Date: 2026-09-27

验收批次B：两层分类树支持——唯一约束从 (kb_id, name) 改为
(kb_id, parent_id, name)，不同主题父分类下可以有同名形态子分类
（如"考勤-制度"与"假期-制度"）。
PostgreSQL 唯一约束中多个 NULL parent_id 视为互异，顶级分类
（主题名由种子脚本去重）不受影响。

注意：downgrade 在已建两层分类树的库上无法直接执行——多个主题下
的同名"制度"子分类必然违反旧 (kb_id, name) 约束。回滚需先手工
合并/删除同名子分类。业务上该迁移只前进不后退。
"""
from alembic import op


revision = "e9c0d12e4f5a"
down_revision = "e7a8b12c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_kb_category_name", "knowledge_categories", type_="unique")
    op.create_unique_constraint(
        "uq_kb_category_parent_name",
        "knowledge_categories",
        ["kb_id", "parent_id", "name"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_kb_category_parent_name", "knowledge_categories", type_="unique")
    op.create_unique_constraint(
        "uq_kb_category_name",
        "knowledge_categories",
        ["kb_id", "name"],
    )
