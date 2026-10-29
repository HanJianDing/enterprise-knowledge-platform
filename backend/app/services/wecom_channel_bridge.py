"""企微通道桥接（批次E·6e 质检修复）：主动推送 + 排重

企微被动回复受 5 秒响应窗口限制，完整问答链路（10~60s）无法同步
返回——回调立即回空响应，问答完成后经"应用消息 API"主动推送
（access_token 由 wecom_secret 换取，进程级缓存 110 分钟）。
MsgId 排重防企微重试风暴（网络超时重试 3 次 → 同消息只答一次）。
"""
import logging
import time
from collections import OrderedDict

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# MsgId 排重缓存（最近 200 条；企微重试窗口内防重复问答）
_seen_msg_ids: OrderedDict = OrderedDict()
_MAX_SEEN = 200


def dedupe_and_mark(msg_id: str) -> bool:
    """MsgId 排重：首次见到返回 True（并记录）；重复返回 False"""
    if not msg_id:
        return True  # 无 MsgId 的消息不排重（宁多答不漏）
    if msg_id in _seen_msg_ids:
        return False
    _seen_msg_ids[msg_id] = time.time()
    while len(_seen_msg_ids) > _MAX_SEEN:
        _seen_msg_ids.popitem(last=False)
    return True


_token_cache: dict = {"token": None, "expires_at": 0.0}


async def _get_access_token() -> str | None:
    """企微 access_token（gettoken 接口，缓存至过期前 5 分钟）"""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": settings.wecom_corp_id,
                        "corpsecret": settings.wecom_secret},
            )
            data = resp.json()
            if data.get("errcode") != 0:
                logger.warning("企微 access_token 获取失败: %s", data)
                return None
            _token_cache["token"] = data["access_token"]
            _token_cache["expires_at"] = time.time() + data.get("expires_in", 7200) - 300
            return _token_cache["token"]
    except Exception as e:
        logger.warning("企微 access_token 请求异常: %s", e)
        return None


def truncate_bytes(text: str, budget: int, tail: str = "…（回答较长，建议登录知识库查看完整内容）") -> str:
    """按字节截断（企微单条消息 ~2048 字节限制）：budget 为总字节预算
    （正文+尾注合计不超）；逐字节回退到完整 UTF-8 字符边界（不残留
    半个字）；短文本原样返回不截。"""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    cut = raw[:budget - len(tail.encode("utf-8"))]
    while cut:
        try:
            cut.decode("utf-8")
            break
        except UnicodeDecodeError:
            cut = cut[:-1]
    return cut.decode("utf-8") + tail


async def push_text_message(to_user: str, content: str) -> bool:
    """应用消息 API 主动推送文本；失败留日志（不抛异常）"""
    token = await _get_access_token()
    if token is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": token},
                json={
                    "touser": to_user,
                    "msgtype": "text",
                    "agentid": int(settings.wecom_agent_id or 0),
                    "text": {"content": content},
                },
            )
            data = resp.json()
            if data.get("errcode") != 0:
                logger.warning("企微消息推送失败: %s", data)
                return False
            return True
    except Exception as e:
        logger.warning("企微消息推送异常: %s", e)
        return False
