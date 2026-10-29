"""时间工具测试 — 统一北京时区口径（上线安全修复）"""
from datetime import datetime, timezone

from app.utils.time import BEIJING_TZ, tz_now, tz_today


class TestTzNow:
    def test_returns_naive_datetime(self):
        """返回 naive 时间（与数据库 DateTime 列存储口径一致）"""
        now = tz_now()
        assert now.tzinfo is None

    def test_beijing_offset_is_utc_plus_8(self):
        """固定时刻换算：UTC 12:00 = 北京 20:00（云服务器常见 UTC，差 8 小时）"""
        utc_noon = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        beijing = utc_noon.astimezone(BEIJING_TZ)
        assert beijing.hour == 20

    def test_today_matches_now_date(self):
        assert tz_today() == tz_now().date()
