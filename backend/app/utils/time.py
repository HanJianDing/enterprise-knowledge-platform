"""时间工具 — 统一北京时区口径（上线修复）

云服务器（含国内云）默认常用 UTC 时区，直接用 datetime.now() 会导致：
- 客服排班判定（"9-21 点上班"）错位 8 小时
- 按天统计的日界在上午 8 点才翻篇
- 消息时间戳与服务器时间混用

统一从这里取时间，代码层面与服务器时区设置解耦。
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def tz_now() -> datetime:
    """当前北京时间（naive，与数据库 DateTime 列存储口径一致）"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def tz_today() -> date:
    """当前北京日期（统计日界、趋势补零用）"""
    return tz_now().date()
