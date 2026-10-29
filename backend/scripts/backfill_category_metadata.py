"""
已有文档迁移脚本 — 将 category_id 补充到 ChromaDB chunk metadata

使用方式:
    cd backend
    python scripts/backfill_category_metadata.py

注意: 此脚本不会重新 embedding，仅更新 ChromaDB 中 existing chunks 的 metadata。
"""
import os
import sys
import asyncio

# 确保 backend 在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.infrastructure.database import async_session_factory
from app.models.document import Document
from app.rag.retriever import get_vector_store


async def backfill():
    """主流程：将 PostgreSQL 中文档的 category_id 同步到 ChromaDB chunk metadata"""
    async with async_session_factory() as session:
        # 1. 查询所有有 category_id 的文档
        result = await session.execute(
            select(Document.id, Document.category_id, Document.original_name)
            .where(Document.category_id.isnot(None))
            .where(Document.category_id != "")
        )
        docs = result.all()
        print(f"找到 {len(docs)} 个有分类的文档")

        if not docs:
            print("没有需要迁移的文档")
            return

    # 2. 获取 ChromaDB collection
    store = get_vector_store()
    collection = store._collection

    updated_count = 0

    for doc_id, category_id, original_name in docs:
        try:
            # 查询该文档的所有 chunks
            chunks = collection.get(
                where={"document_id": doc_id},
                include=["metadatas"],
            )

            if not chunks or not chunks.get("ids"):
                print(f"  ⚠ 文档 {original_name} ({doc_id}) 在 ChromaDB 中无 chunk，跳过")
                continue

            chunk_ids = chunks["ids"]
            old_metadatas = chunks.get("metadatas", [])
            new_metadatas = []

            for meta in old_metadatas:
                new_meta = dict(meta)
                new_meta["category_id"] = category_id
                new_metadatas.append(new_meta)

            # 批量更新 metadata（不含 vectors，不重新 embedding）
            collection.update(
                ids=chunk_ids,
                metadatas=new_metadatas,
            )

            print(f"  ✅ {original_name}: 已更新 {len(chunk_ids)} 个 chunks (category_id={category_id})")
            updated_count += 1

        except Exception as e:
            print(f"  ❌ {original_name} ({doc_id}): {e}")

    print(f"\n迁移完成: {updated_count}/{len(docs)} 个文档已更新")


if __name__ == "__main__":
    asyncio.run(backfill())
