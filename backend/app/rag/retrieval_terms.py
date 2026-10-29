"""检索提准·同义词表 + 词权重（验收批次1 子项1a）

配置存 JSON 文件（backend/config/retrieval_terms.json），管理页 API 读写：
- synonyms：查询词 → 文档中的同义变体列表（如 礼品→礼金、报销→费用报销），
  BM25 腿在查询时做词扩展，解决"问法不同导致词面漏配"（礼品题回归的驱动案例）
- weights：词 → 权重倍数（>1 放大、<1 降权该词的匹配贡献），BM25 打分侧逐词加权

选文件而非数据库：单 worker 部署下文件简单可版本化，且 BM25 查询路径是
同步线程内执行（检索链路在 to_thread 中），文件读取零 IO 依赖、带 mtime
缓存，不阻塞事件循环、不引入跨线程 DB 访问。
"""
import json
import os
import threading
from pathlib import Path

import jieba

TERMS_FILE = Path(__file__).resolve().parents[2] / "config" / "retrieval_terms.json"

# mtime 缓存：文件未变则直接复用内存配置（读文件仅一次 stat，开销极小）
_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float | None = None


def _default() -> dict:
    return {"synonyms": {}, "weights": {}}


def _load() -> dict:
    """读配置（带 mtime 缓存，线程安全）"""
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(TERMS_FILE)
    except OSError:
        mtime = None
    with _lock:
        if _cache is not None and _cache_mtime == mtime:
            return _cache
        if mtime is None:
            _cache = _default()
        else:
            try:
                data = json.loads(TERMS_FILE.read_text(encoding="utf-8"))
                _cache = {
                    "synonyms": {str(k): [str(s) for s in v] for k, v in data.get("synonyms", {}).items()},
                    "weights": {str(k): float(v) for k, v in data.get("weights", {}).items()},
                }
            except (json.JSONDecodeError, OSError):
                _cache = _default()
        _cache_mtime = mtime
        return _cache


def invalidate() -> None:
    """配置写入后调用（管理页 API 侧），强制下次查询重读文件"""
    global _cache_mtime
    with _lock:
        _cache_mtime = None


def expand_terms(query: str) -> list[tuple[str, float]]:
    """查询扩展：返回 [(词, 权重)] 列表——同义词展开 + 词权重。

    - 同义词变体继承原词权重（激活实验修正：变体不应是二等公民——问"礼品"
      制度写"礼金"时，礼金 若只拿 1.0 权重会被"礼券"类近义块淹没，实测
      行为准则块因此掉到第 37 名；继承后同权重竞争才公平）
    - 权重 >1 放大、<1 降权（验收批次1·8a：降权由 BM25 打分侧逐词加权实现，
      替代旧的"重复加权词"土办法——rank_bm25 不支持逐词权重，已换自写打分）
    - 配置为空时返回原词各权重 1.0（行为与无此功能完全一致）
    """
    terms = _load()
    out: list[tuple[str, float]] = []
    for token in jieba.cut(query):
        token = token.strip()
        if not token:
            continue
        weight = terms["weights"].get(token, 1.0)
        weight = weight if weight > 0 else 1.0
        out.append((token, weight))
        for syn in terms["synonyms"].get(token, []):
            if syn:
                out.append((syn, weight))
    return out


def save_terms(data: dict) -> None:
    """管理页 API 写入：整体替换配置并失效缓存。

    质检低危修复（并发采纳/保存丢更新）：写盘与失效纳入模块级 _lock，
    与读取路径互斥；单管理员低频操作，锁开销可忽略。
    注意：不调 invalidate()（它自己会拿 _lock，非重入锁会死锁），
    在锁内直接置空 mtime 缓存等效。
    """
    global _cache_mtime
    with _lock:
        TERMS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _cache_mtime = None
