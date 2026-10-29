"""差旅安全文档重归类脚本（验收批次E，对应质检建议级问题 16）

背景：seed_demo.theme_of 旧逻辑先判"差旅"，把《差旅安全》系列 4 份文档
误归入"差旅"主题分类。theme_of 已修复（先判"差旅安全"），本脚本把两个
环境中已入库的差旅安全文档重挂到"差旅安全"主题分类下：
- 建父分类"差旅安全"（同一知识库内）+ 形态子分类（制度/流程/表单/FAQ）
- 文档重挂后删除空出来的旧子分类与旧"差旅"父分类（仅当已无文档）

幂等：重跑无副作用。演示/真实隔离与 seed_demo.py 同款（--target real 需 --yes）。

用法（backend 目录下）：
    python scripts/reclassify_travel_safety.py                  # 演示隔离库
    python scripts/reclassify_travel_safety.py --target real --yes   # 真实库
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT = BACKEND_DIR.parent

_TARGET_PARSER = argparse.ArgumentParser(add_help=False)
_TARGET_PARSER.add_argument("--target", choices=["demo", "real"], default="demo")
_TARGET_PARSER.add_argument("--yes", action="store_true")
_target_args, _ = _TARGET_PARSER.parse_known_args()
TARGET_REAL = _target_args.target == "real"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env(ROOT / ".env")
import urllib.parse

if not TARGET_REAL:
    _load_env(ROOT / ".env.demo")
    _real_url = os.environ.get("DATABASE_URL", "")
    _parsed = urllib.parse.urlparse(_real_url)
    os.environ["DATABASE_URL"] = _parsed._replace(path="/ragdb_demo").geturl()
    os.environ["CHROMA_PERSIST_DIR"] = str(BACKEND_DIR / "chroma_demo")
    os.environ["UPLOAD_DIR"] = str(BACKEND_DIR / "uploads_demo")

sys.path.insert(0, str(BACKEND_DIR))


async def main() -> None:
    from sqlalchemy import select

    from app.infrastructure.database import async_session_factory
    from app.models.document import Document, KnowledgeCategory

    async with async_session_factory() as db:
        cats = (await db.execute(select(KnowledgeCategory))).scalars().all()
        docs = (await db.execute(
            select(Document).where(Document.original_name.like("差旅安全%"))
        )).scalars().all()

    if not docs:
        print("没有差旅安全文档，无需重归类")
        return

    # 按 (kb, 旧父分类) 分组：差旅安全文档当前挂在哪些分类下
    by_key: dict[tuple[str, str], list[tuple]] = {}
    for d in docs:
        sub = next((c for c in cats if c.id == d.category_id), None)
        if sub is None or sub.parent_id is None:
            print(f"  ⚠ {d.original_name} 没有两层分类，跳过")
            continue
        by_key.setdefault((d.kb_id, sub.parent_id), []).append((d.id, d.original_name, sub))

    moved = removed_cats = 0
    for (kb_id, old_parent_id), entries in by_key.items():
        async with async_session_factory() as db:
            old_parent = (await db.execute(
                select(KnowledgeCategory).where(KnowledgeCategory.id == old_parent_id)
            )).scalar_one()
            # 幂等：父分类按名称查找/创建
            new_parent = (await db.execute(
                select(KnowledgeCategory).where(
                    KnowledgeCategory.kb_id == kb_id,
                    KnowledgeCategory.parent_id.is_(None),
                    KnowledgeCategory.name == "差旅安全",
                )
            )).scalar_one_or_none()
            if new_parent is None:
                new_parent = KnowledgeCategory(kb_id=kb_id, name="差旅安全")
                db.add(new_parent)
                await db.flush()

            # 形态子分类按名称复用；文档在本会话内加载后再改（防 detached 实例无效写入）
            sub_rows = (await db.execute(
                select(KnowledgeCategory).where(KnowledgeCategory.parent_id == new_parent.id)
            )).scalars().all()
            name_to_sub = {c.name: c for c in sub_rows}
            target_id: dict[str, str] = {}
            old_sub_ids: set[str] = set()
            for doc_id, _name, sub in entries:
                old_sub_ids.add(sub.id)
                target = name_to_sub.get(sub.name)
                if target is None:
                    target = KnowledgeCategory(
                        kb_id=kb_id, parent_id=new_parent.id, name=sub.name,
                    )
                    db.add(target)
                    await db.flush()
                    name_to_sub[sub.name] = target
                target_id[doc_id] = target.id
            doc_rows = (await db.execute(
                select(Document).where(Document.id.in_(list(target_id)))
            )).scalars().all()
            for row in doc_rows:
                if row.category_id != target_id[row.id]:
                    row.category_id = target_id[row.id]
                    moved += 1

            # 旧子分类与旧"差旅"父分类：已无文档才删除（防误删共享分类）
            for old_sub_id in old_sub_ids:
                remain = (await db.execute(
                    select(Document.id).where(Document.category_id == old_sub_id).limit(1)
                )).first()
                if remain is None:
                    cat = (await db.execute(
                        select(KnowledgeCategory).where(KnowledgeCategory.id == old_sub_id)
                    )).scalar_one_or_none()
                    if cat is not None:
                        await db.delete(cat)
                        removed_cats += 1
            kids_left = (await db.execute(
                select(KnowledgeCategory).where(KnowledgeCategory.parent_id == old_parent.id)
            )).scalars().all()
            remain_under_old_parent = (await db.execute(
                select(Document.id)
                .where(Document.category_id.in_(
                    select(KnowledgeCategory.id).where(
                        KnowledgeCategory.parent_id == old_parent.id
                    )
                ))
                .limit(1)
            )).first()
            if remain_under_old_parent is None:
                # 旧父分类下已无任何文档 → 清掉剩余空子分类与旧父分类本身
                for k in kids_left:
                    await db.delete(k)
                    removed_cats += 1
                await db.delete(old_parent)
                removed_cats += 1

            await db.commit()
            print(f"  知识库 {kb_id}：{len(entries)} 份差旅安全文档重挂到"
                  f"「差旅安全」主题（旧主题「{old_parent.name}」清理完成）")

    print(f"重归类完成：移动文档 {moved} 份，删除空分类 {removed_cats} 个")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="差旅安全文档重归类（验收批次E）")
    parser.add_argument("--target", choices=["demo", "real"], default="demo",
                        help="real=真实环境（需 --yes 确认）；demo=演示隔离库（默认）")
    parser.add_argument("--yes", action="store_true", help="确认对真实库执行")
    args = parser.parse_args()
    if args.target == "real" and not args.yes:
        parser.error("--target real 会修改真实库分类数据，需加 --yes 确认")
    asyncio.run(main())
