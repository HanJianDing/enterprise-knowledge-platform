"""Web 搜索兜底（批次A·11f）——知识库答不了的问题接外部搜索

设计（用户 2026-10 拍板）：
- 触发条件：知识库检索无结果（chat_service no_docs 分支）时兜底
- 合规开关：settings.web_search_enabled + WEB_SEARCH_API_KEY——
  真实企业内网部署默认关闭（数据不出域原则）；演示环境配 key 即开
- provider：博查 Web Search API（国内服务，免费额度够演示）；
  未来换源（Bing/Tavily）只需新增 provider 函数

博查 API：POST https://api.bochaai.com/v1/web-search
  Header: Authorization: Bearer <key>
  Body: {"query": ..., "count": 5, "freshness": "noLimit", "summary": true}

批次F 前置小批（质检 P2 缺口补齐）：
- 进程级限速：每分钟最多 web_search_rate_per_min 次（低相关判定扩宽
  触发面后，弱相关问题也出网——防连锁误触发烧额度）
- 结果缓存：相同 query 结果缓存 300s（兜底问题重复率高——同一批
  用户反复问同一类知识库外问题；缓存容量上限防内存膨胀）
"""
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BOCHA_URL = "https://api.bochaai.com/v1/web-search"

# 限速：滑动 60s 窗口（进程级，多 worker 部署时各进程独立计数——
# 上限是软保护非硬承诺，见 docstring）
_rate_calls: list[float] = []
# 缓存：query → (写入时间戳, 结果)；容量上限 128 条（LRU 淘汰语义
# 用"满则整体清空"近似——容量小、频率低，简单优先）
_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 300.0
_CACHE_MAX = 128


def _cache_get(query: str) -> list[dict] | None:
    """命中且未过期的缓存结果；过期条目就地清除。
    返回浅拷贝（留档 P3-3）：防消费方就地修改污染缓存条目。"""
    hit = _cache.get(query)
    if hit is None:
        return None
    ts, results = hit
    if time.monotonic() - ts > _CACHE_TTL:
        del _cache[query]
        return None
    return list(results)


def _cache_put(query: str, results: list[dict]) -> None:
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[query] = (time.monotonic(), results)


def _rate_limited() -> bool:
    """60s 窗口内调用数是否已达上限（顺带清理窗口外旧记录）"""
    limit = settings.web_search_rate_per_min
    now = time.monotonic()
    _rate_calls[:] = [t for t in _rate_calls if now - t < 60.0]
    if len(_rate_calls) >= limit:
        return True
    _rate_calls.append(now)
    return False


async def search_web(query: str, count: int = 5) -> list[dict]:
    """外部网络搜索。未启用/无 key/失败/超时/限速一律返回空列表
    （静默降级——兜底功能不允许拖垮或报错主链路）。
    返回 [{title, url, snippet}]；相同 query 在缓存 TTL 内直接复用结果。"""
    if not settings.web_search_enabled or not settings.web_search_api_key:
        return []
    cached = _cache_get(query)
    if cached is not None:
        return cached
    if _rate_limited():
        logger.warning(f"Web 搜索限速触发（{settings.web_search_rate_per_min}/min），"
                       "本次返回空")
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _BOCHA_URL,
                headers={"Authorization": f"Bearer {settings.web_search_api_key}"},
                json={"query": query, "count": count,
                      "freshness": "noLimit", "summary": True},
            )
            if resp.status_code != 200:
                logger.warning(f"Web 搜索失败（HTTP {resp.status_code}）: "
                               f"{resp.text[:200]}")
                return []
            data = resp.json()
            pages = (data.get("data") or {}).get("webPages") or {}
            out = []
            for p in (pages.get("value") or [])[:count]:
                snippet = (p.get("summary") or p.get("snippet") or "").strip()
                if not snippet:
                    continue
                out.append({
                    "title": p.get("name", ""),
                    "url": p.get("url", ""),
                    "snippet": snippet[:400],
                })
            # 缓存存浅拷贝（质检 P3-5）：返回与缓存不共享同一列表对象，
            # 防消费方就地修改污染缓存
            _cache_put(query, list(out))
            return out
    except Exception as e:
        logger.warning(f"Web 搜索异常（静默降级）: {e}")
        return []
