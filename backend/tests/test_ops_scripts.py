"""产品化 C 测试：运维脚本纯函数 + /health 深度检查 + 审计清理"""
import pytest
from httpx import AsyncClient

from scripts.backup import cleanup_old_backups, parse_db_url, resolve_db_url
from scripts.health_check import build_message


# ════════════════════════════════════════════════════
# backup.py 纯函数
# ════════════════════════════════════════════════════

class TestBackupScript:
    def test_cleanup_old_backups_keeps_recent(self, tmp_path):
        """保留最近 N 份：时间戳最旧目录被删，最新保留"""
        for stamp in ["20260101_000000", "20260102_000000", "20260103_000000"]:
            (tmp_path / stamp).mkdir()
        removed = cleanup_old_backups(tmp_path, keep=2)
        assert removed == 1
        assert not (tmp_path / "20260101_000000").exists()
        assert (tmp_path / "20260103_000000").exists()

    def test_cleanup_keep_zero_removes_all(self, tmp_path):
        (tmp_path / "20260101_000000").mkdir()
        removed = cleanup_old_backups(tmp_path, keep=0)
        assert removed == 1
        assert not (tmp_path / "20260101_000000").exists()

    def test_cleanup_missing_dir_returns_zero(self, tmp_path):
        assert cleanup_old_backups(tmp_path / "nope", keep=14) == 0

    def test_parse_db_url(self):
        info = parse_db_url("postgresql+asyncpg://user:secret@db:5432/ragdb")
        assert info == {
            "host": "db", "port": "5432", "user": "user",
            "password": "secret", "db": "ragdb",
        }

    def test_parse_db_url_percent_decoded(self):
        """特殊字符密码 percent-decode（不解码会导致 pg_dump 认证静默失败）"""
        info = parse_db_url("postgresql+asyncpg://user:p%40ss%2Fword@db:5432/ragdb")
        assert info["password"] == "p@ss/word"

    def test_resolve_db_url_env_first(self, monkeypatch):
        """验收 A7：环境变量优先于 .env 文件"""
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://env@db:5432/x")
        assert resolve_db_url() == "postgresql+asyncpg://env@db:5432/x"

    def test_resolve_db_url_falls_back_to_env_file(self, monkeypatch, tmp_path):
        """验收 A7：未设环境变量时回退读 .env 文件（此前顺序颠倒永不生效）"""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("DATABASE_URL=postgresql+asyncpg://file@db:5432/y\n", encoding="utf-8")
        assert resolve_db_url(env_file=env_file) == "postgresql+asyncpg://file@db:5432/y"

    def test_resolve_db_url_empty_when_nowhere(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert resolve_db_url(env_file=tmp_path / "nope.env") == ""


# ════════════════════════════════════════════════════
# health_check.py 纯函数
# ════════════════════════════════════════════════════

class TestHealthScript:
    def test_build_message_degraded(self):
        msg = build_message({"status": "degraded", "checks": {"database": True, "vector_store": False}})
        assert "告警" in msg
        assert "vector_store" in msg

    def test_build_message_unreachable(self):
        msg = build_message({"status": "unreachable", "checks": {}})
        assert "请求失败" in msg


# ════════════════════════════════════════════════════
# /health 深度检查（C4）
# ════════════════════════════════════════════════════

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_reports_real_checks(self, client: AsyncClient):
        """/health 返回数据库/向量库真实连通性（不再是写死的 healthy）"""
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["checks"]["database"] is True
        assert data["checks"]["vector_store"] is True
        assert set(data["checks"].keys()) == {"database", "vector_store"}


# ════════════════════════════════════════════════════
# 审计保留策略（C3）
# ════════════════════════════════════════════════════

class TestAuditPurge:
    @pytest.mark.asyncio
    async def test_purge_removes_only_old_logs(self, client: AsyncClient):
        """清理只删旧日志，新日志保留"""
        from datetime import timedelta

        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.audit import AuditLog
        from app.utils.time import tz_now

        from scripts.purge_audit_logs import purge_old_logs

        async with async_session_factory() as db:
            db.add(AuditLog(
                user_id="u1", username="old_user", action="user.login",
                created_at=tz_now() - timedelta(days=120),
            ))
            db.add(AuditLog(
                user_id="u2", username="new_user", action="user.login",
                created_at=tz_now(),
            ))
            await db.commit()

        removed = await purge_old_logs(keep_days=90)
        assert removed == 1

        async with async_session_factory() as db:
            remaining = (await db.execute(select(AuditLog))).scalars().all()
        assert [log.username for log in remaining] == ["new_user"]
