"""批次11a RAPTOR 摘要树诊断/触发脚本

用法（在 backend 目录下）：
    python scripts/diag_raptor.py --list          # 列出厚文档（>raptor_min_chars）与建树状态
    python scripts/diag_raptor.py --build all     # 对全部厚文档建树（真实嵌入+LLM 调用）
    python scripts/diag_raptor.py --show          # 展示已建树的结构（层级/簇/摘要块）
    python scripts/diag_raptor.py --drop <doc_id> # 删除指定文档的摘要树
"""
import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


async def _list_long_docs() -> list[dict]:
    """Chroma 侧按文档聚合字数，找出超过 raptor_min_chars 的文档"""
    from app.config import settings
    from app.rag.retriever import get_vector_store

    collection = get_vector_store()._collection
    r = collection.get(include=["documents", "metadatas"])
    docs, metas = r.get("documents") or [], r.get("metadatas") or [{} for _ in docs]
    by_doc: dict[str, dict] = {}
    for content, meta in zip(docs, metas):
        did = meta.get("document_id", "")
        if not did:
            continue
        entry = by_doc.setdefault(did, {"len": 0, "chunks": 0, "name": "", "summary": 0})
        entry["len"] += len(content or "")
        entry["chunks"] += 1
        entry["name"] = meta.get("filename", "")
        if meta.get("chunk_type") == "raptor_summary":
            entry["summary"] += 1
    out = [
        {"doc_id": did, **v}
        for did, v in by_doc.items()
        if v["len"] >= settings.raptor_min_chars
    ]
    out.sort(key=lambda d: -d["len"])
    return out


async def _build(doc_ids: list[str], filenames: dict[str, str]) -> None:
    from app.rag import raptor

    for did in doc_ids:
        name = filenames.get(did, did)
        print(f"\n建树: {name} ({did}) ...")
        result = await raptor.build_tree_for_document(did, name)
        print(f"  → {result}")


async def _show() -> None:
    from sqlalchemy import select
    from app.infrastructure.database import async_session_factory
    from app.models.raptor_cluster import RaptorCluster

    async with async_session_factory() as db:
        rows = (await db.execute(select(RaptorCluster))).scalars().all()
    if not rows:
        print("暂无摘要树（先 --build）")
        return
    by_doc: dict[str, list] = {}
    for r in rows:
        by_doc.setdefault(r.document_id, []).append(r)
    for did, doc_rows in by_doc.items():
        by_level: dict[int, list] = {}
        for r in doc_rows:
            by_level.setdefault(r.tree_level, []).append(r)
        print(f"\n文档 {did}: {len(doc_rows)} 簇 / {len(by_level)} 层")
        for level in sorted(by_level):
            for r in by_level[level]:
                kids = len(r.child_chunk_indices or [])
                print(f"  L{level} {r.cluster_id}: 摘要块#{r.summary_chunk_index} ← {kids} 个子块")


async def _drop(doc_id: str) -> None:
    from app.rag import raptor

    removed = await raptor.delete_tree_for_document(doc_id)
    print(f"簇表清理 {removed} 行。Chroma 摘要块仍需清理：对文档 reprocess --force"
          f" 重建（幂等清理会先删旧摘要块），或直接删除文档")


def main() -> None:
    parser = argparse.ArgumentParser(description="RAPTOR 摘要树诊断")
    parser.add_argument("--list", action="store_true", help="列出厚文档")
    parser.add_argument("--build", metavar="doc_id|all", help="建树")
    parser.add_argument("--show", action="store_true", help="展示树结构")
    parser.add_argument("--drop", metavar="doc_id", help="删除摘要树")
    args = parser.parse_args()

    if args.list:
        docs = asyncio.run(_list_long_docs())
        print(f"厚文档（>阈值）共 {len(docs)} 份：")
        for d in docs:
            state = f"已建树({d['summary']} 摘要块)" if d["summary"] else "未建树"
            print(f"  {d['len']:>7} 字 / {d['chunks']:>3} 块 / {d['name']:<24} [{state}]  {d['doc_id']}")
        return

    if args.build:
        docs = asyncio.run(_list_long_docs())
        if args.build == "all":
            targets = docs
        else:
            targets = [d for d in docs if d["doc_id"] == args.build]
            if not targets:
                print(f"doc_id {args.build} 不在厚文档列表（先 --list 查看）")
                return
        asyncio.run(_build(
            [d["doc_id"] for d in targets],
            {d["doc_id"]: d["name"] for d in docs},
        ))
        return

    if args.show:
        asyncio.run(_show())
        return

    if args.drop:
        asyncio.run(_drop(args.drop))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
