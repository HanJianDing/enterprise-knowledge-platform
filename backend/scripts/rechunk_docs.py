"""全库重切脚本（验收批次E）：用当前分块模板重新分块受影响的文档。

背景：chunk_laws 修复后（章标题并入正文 + 按章分块），库中已分块的
条款类文档结构过期（章名缺失、整篇一块、块标签名不副实）。本脚本找出
分块结果会变化的文档（long_text 且条款结构检测通过），逐一 force 重切——
幂等，重跑无害；不重切无关文档，避免浪费嵌入 API 调用。

演示/真实隔离与 seed_demo.py 同款：--target demo（默认）用隔离环境，
--target real 需 --yes 确认。

用法（backend 目录下）：
    python scripts/rechunk_docs.py                  # 演示隔离库
    python scripts/rechunk_docs.py --target real --yes   # 真实库
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT = BACKEND_DIR.parent

# 先解析 --target（在环境隔离之前决定指向哪个库）
_TARGET_PARSER = argparse.ArgumentParser(add_help=False)
_TARGET_PARSER.add_argument("--target", choices=["demo", "real"], default="demo")
_TARGET_PARSER.add_argument("--yes", action="store_true")
_target_args, _ = _TARGET_PARSER.parse_known_args()
TARGET_REAL = _target_args.target == "real"


# ── 环境设置（必须在 import app 之前，与 seed_demo.py 同款） ──
def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env(ROOT / ".env")  # 真实密钥（DASHSCOPE_API_KEY 与真实库地址）
import urllib.parse

if not TARGET_REAL:
    # 演示模式：隔离变量指向演示库/目录（与 seed_demo.py 同款）
    _load_env(ROOT / ".env.demo")
    _real_url = os.environ.get("DATABASE_URL", "")
    _parsed = urllib.parse.urlparse(_real_url)
    os.environ["DATABASE_URL"] = _parsed._replace(path="/ragdb_demo").geturl()
    os.environ["CHROMA_PERSIST_DIR"] = str(BACKEND_DIR / "chroma_demo")
    os.environ["UPLOAD_DIR"] = str(BACKEND_DIR / "uploads_demo")

sys.path.insert(0, str(BACKEND_DIR))

_TEXT_EXTS = {".txt", ".md"}


def _needs_rechunk(doc) -> bool:
    """判断分块结果是否会随新模板变化：
    - long_text（验收批次1·1c：全部 long_text 过父子双层分块——
      超长父块产子块，无条款结构也受影响，整类重切）
    - faq 且首问前有标题/前言且问答对 ≥2（chunk_faq 前言保留修复）

    文本类（txt/md）直接读文件判定（零 API 成本）；其他扩展名无法廉价
    判定，保守重切。物理文件缺失的文档跳过（reprocess 会 400）。
    """
    if doc.content_type not in ("long_text", "faq"):
        return False
    if not doc.storage_path or not os.path.exists(doc.storage_path):
        return False
    if Path(doc.storage_path).suffix.lower() not in _TEXT_EXTS:
        return True
    try:
        text = Path(doc.storage_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True
    if doc.content_type == "long_text":
        # 1f：章节 ≥10 的条款文档需重切（追加"目录摘要块"）
        import re as _re

        chapters = _re.findall(r"第[一二三四五六七八九十百]+章", text)
        if len(set(chapters)) >= 10:
            return True
        # 1c 激活：厚文档（>阈值）需重切（子分块自动启用条件）——总闸关时跳过
        from app.config import settings

        return settings.retrieval_subchunk_enabled and len(text) > settings.retrieval_subchunk_min_chars
    from app.rag.chunker import _detect_qa_pairs

    parsed = _detect_qa_pairs(text)
    return parsed is not None and bool(parsed[0])


async def main() -> None:
    from sqlalchemy import select

    from app.infrastructure.database import async_session_factory
    from app.models.document import Document
    from app.services import document_service

    async with async_session_factory() as db:
        docs = list((await db.execute(
            select(Document).where(Document.status == "completed")
        )).scalars().all())

    targets = [d for d in docs if _needs_rechunk(d)]
    env_name = "真实库（--target real）" if TARGET_REAL else "演示隔离库"
    print(f"目标环境：{env_name}")
    print(f"完成态文档共 {len(docs)} 份，需重切 {len(targets)} 份"
          f"（其余 {len(docs) - len(targets)} 份分块结果不受影响，跳过）")

    ok = failed = 0
    for i, doc in enumerate(targets, 1):
        try:
            async with async_session_factory() as db:
                await document_service.reprocess_document(db, doc.id, force=True)
            # 等处理完成（后台任务 + 嵌入 API；逐份等待避免并发打爆限流）
            deadline = time.monotonic() + 300
            while document_service._in_flight_doc_ids and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
            async with async_session_factory() as db:
                row = (await db.execute(
                    select(Document).where(Document.id == doc.id)
                )).scalar_one()
            if row.status != "completed":
                failed += 1
                print(f"  [{i}/{len(targets)}] ✗ {doc.original_name}:"
                      f" 重切未完成（状态 {row.status}）")
                continue
            ok += 1
            print(f"  [{i}/{len(targets)}] ✓ {doc.original_name} → {row.chunk_count} 块")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(targets)}] ✗ {doc.original_name}: {e}")
    print(f"重切完成：成功 {ok}，失败 {failed}，共 {len(targets)} 份")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="全库重切脚本（验收批次E）")
    parser.add_argument("--target", choices=["demo", "real"], default="demo",
                        help="real=真实环境（需 --yes 确认）；demo=演示隔离库（默认）")
    parser.add_argument("--yes", action="store_true", help="确认对真实库执行重切")
    args = parser.parse_args()
    if args.target == "real" and not args.yes:
        parser.error("--target real 会重切真实库全部受影响文档，需加 --yes 确认")
    asyncio.run(main())
