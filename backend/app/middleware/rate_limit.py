"""限频 — 防公网爆破与 LLM 配额滥用（上线安全修复）

内存固定窗口计数器（60 秒）：登录 5 次/分/IP（批次F 验收恢复并配置化
LOGIN_RATE_LIMIT_PER_MIN；批次5.5 压测曾提到 30）、对话 10 次/分/用户。
单进程部署与现有架构一致（向量库本身约束单 worker），内存实现足够；
多实例部署需换成 Redis 等共享存储（当前无此需求）。

企业知识库改造阶段3：
- 注册限频已随开放注册关闭移除；
- 对话限频从"按 IP"改为"按用户"——企业出口 IP 收敛（全公司一个公网 IP）
  时按 IP 限频会误伤全公司，按登录用户限频才是正确口径。

测试环境通过 RATE_LIMIT_ENABLED=false 关闭（conftest 已设置），
避免测试间的全局计数互相干扰。
"""
import time
from collections import defaultdict

from fastapi import Depends, HTTPException, Request

from app.config import settings
from app.middleware.auth import get_current_user
from app.models.user import User

WINDOW_SECONDS = 60.0


class RateLimiter:
    """固定窗口计数器：window 秒内每个 key 最多 limit 次"""

    def __init__(self, window: float = WINDOW_SECONDS):
        self.window = window
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        window_start = now - self.window
        hits = [t for t in self._hits[key] if t > window_start]
        self._hits[key] = hits
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


_limiter = RateLimiter()


def reset_limiter() -> None:
    """测试用：清空全部计数"""
    _limiter.reset()


def _client_ip(request: Request) -> str:
    """nginx 反代后取真实 IP：优先 X-Forwarded-For 首项，否则直连 IP"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _check(request: Request, name: str, limit: int) -> None:
    if not settings.rate_limit_enabled:
        return
    key = f"{name}:{_client_ip(request)}"
    if not _limiter.allow(key, limit):
        raise HTTPException(status_code=429, detail="操作太频繁，请稍后再试")


async def login_rate_limit(request: Request) -> None:
    """登录限频：同一 IP 每分钟最多 settings.login_rate_limit_per_min 次
    （默认 5，防弱口令爆破）。

    阈值配置化（批次F 验收）：批次5.5 压测修复曾把写死的 5 提到 30
    （perf01~20 压测账号并行登录被 5 误伤）；2026-10-13 用户拍板恢复
    5——压测已结束、防爆破从严；企业出口 IP 收敛场景下如需放宽
    在 .env 配 LOGIN_RATE_LIMIT_PER_MIN 即可。
    前提：本系统部署于企业内网（质检 P2-1）——公网部署建议叠加
    账号维度失败锁定。"""
    await _check(request, "login", settings.login_rate_limit_per_min)


async def chat_rate_limit(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> None:
    """对话限频：同一用户每分钟最多 10 次（防脚本刷 LLM 计费）。

    阶段3 改按用户：企业出口 IP 收敛，按 IP 会把全公司一起限了。
    未认证请求在鉴权处 401 短路、不会触发任何 LLM 计费，无需计数。
    """
    if not settings.rate_limit_enabled:
        return
    key = f"chat:{current_user.id}"
    if not _limiter.allow(key, 10):
        raise HTTPException(status_code=429, detail="操作太频繁，请稍后再试")


async def sso_callback_rate_limit(request: Request) -> None:
    """SSO 回调限频（产品化 C6 backlog LOW 项）：同一 IP 每分钟最多 30 次。

    回调端点无认证门槛（浏览器刚完成扫码跳回），需防恶意脚本刷回调换身份；
    阈值取 30 而非 10——企业出口 IP 收敛（全公司一个公网 IP），早高峰集中
    扫码登录时按 IP 过紧会把全公司 429（与 chat 限频改按用户的教训同源）。
    """
    await _check(request, "sso_callback", 30)
