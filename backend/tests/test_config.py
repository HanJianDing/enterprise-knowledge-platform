"""配置模块测试"""
import pytest
from app.config import Settings


class TestSettings:
    """Settings 配置测试"""

    def test_default_values(self):
        """测试默认配置值"""
        settings = Settings()
        assert settings.llm_model == "qwen-plus"
        assert settings.llm_temperature == 0.1
        assert settings.embedding_model == "text-embedding-v2"
        assert settings.jwt_algorithm == "HS256"
        assert settings.access_token_expire_minutes == 15
        assert settings.refresh_token_expire_days == 7
        assert settings.backend_port == 8000
        assert settings.frontend_port == 5173
        assert settings.max_upload_size == 20 * 1024 * 1024

    def test_token_expiry_types(self):
        """测试 Token 过期时间为整数"""
        settings = Settings()
        assert isinstance(settings.access_token_expire_minutes, int)

    def test_rate_limit_and_cors_config(self):
        """上线安全修复：限频可开关（测试环境 conftest 置 false）；
        CORS 放行来源为逗号分隔可配置串，显式传值可覆盖"""
        settings = Settings()
        # 测试环境由 conftest 关闭限频（进程级计数会污染用例）
        assert settings.rate_limit_enabled is False
        assert Settings(rate_limit_enabled=True).rate_limit_enabled is True
        origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
        assert "http://localhost:5173" in origins
        assert "http://localhost:8501" in origins
        assert isinstance(settings.refresh_token_expire_days, int)
        assert settings.access_token_expire_minutes > 0
        assert settings.refresh_token_expire_days > 0

    def test_max_upload_size_reasonable(self):
        """测试最大上传大小合理"""
        settings = Settings()
        assert settings.max_upload_size > 0
        assert settings.max_upload_size <= 100 * 1024 * 1024  # 不超过 100MB

    def test_port_values_reasonable(self):
        """测试端口值合理"""
        settings = Settings()
        assert 1024 < settings.backend_port < 65536
        assert 1024 < settings.frontend_port < 65536

    def test_database_url_format(self):
        """测试数据库 URL 格式"""
        settings = Settings()
        # 默认使用 PostgreSQL + asyncpg
        assert "postgresql+asyncpg" in settings.database_url or "sqlite" in settings.database_url

    def test_chroma_persist_dir(self):
        """测试 ChromaDB 持久化目录"""
        settings = Settings()
        assert "chroma" in settings.chroma_persist_dir.lower()

    def test_upload_dir(self):
        """测试上传目录"""
        settings = Settings()
        assert "upload" in settings.upload_dir.lower()

    def test_jwt_secret_key_accepts_string(self):
        """测试 JWT secret key 接受字符串"""
        settings = Settings()
        assert isinstance(settings.jwt_secret_key, str)

    def test_empty_jwt_secret_key_rejected(self):
        """安全修复回归：JWT 密钥为空时拒绝实例化（防止空串签名 token）"""
        import pytest as _pytest
        with _pytest.raises(ValueError, match="JWT_SECRET_KEY"):
            Settings(jwt_secret_key="", _env_file=None)

    def test_empty_database_url_rejected(self):
        """安全修复回归：数据库连接串为空时拒绝实例化"""
        import pytest as _pytest
        with _pytest.raises(ValueError, match="DATABASE_URL"):
            Settings(database_url="", _env_file=None)

    def test_reflection_retries_clamped(self):
        """批次C.5：反思重试次数钳制 0~4——误配大数（烧钱）/负数（语义错）
        被钳制到安全区间"""
        from app.config import Settings as _S

        assert _S(agent_reflection_max_retries=100).agent_reflection_max_retries == 4
        assert _S(agent_reflection_max_retries=-3).agent_reflection_max_retries == 0
        assert _S(agent_reflection_max_retries=2).agent_reflection_max_retries == 2
        assert _S().agent_reflection_max_retries == 1  # 默认保守值
