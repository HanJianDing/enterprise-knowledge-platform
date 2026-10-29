"""数据库连接管理 - SQLAlchemy Async Engine + Session"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.config import settings

# SQLite（开发/测试）：
# - StaticPool 单连接——所有会话（含审计/上传后台任务的独立会话）串行化
#   走同一条连接，锁竞争从物理上不存在（"database is locked" 的根治）；
# - 共享内存库（file:...mode=memory&cache=shared）只在连接存续期存在，
#   单连接常驻即整库常驻，测试无需清理磁盘文件。
# SQLite（开发/测试）：
# - StaticPool 单连接——所有会话（含审计/上传后台任务的独立会话）串行化
#   走同一条连接，锁竞争从物理上不存在（"database is locked" 的根治）；
#   注意 StaticPool 不接受 pool_size/max_overflow，SQLite 分支不传这两项；
# - 共享内存库（file:...mode=memory&cache=shared）只在连接存续期存在，
#   单连接常驻即整库常驻，测试无需清理磁盘文件。
_is_sqlite = settings.database_url.startswith("sqlite")
_engine_kwargs: dict = {}
if _is_sqlite:
    _engine_kwargs["poolclass"] = StaticPool
    _engine_kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    # 批次5 压测修复：pool 20+10 在 SSE 长请求（生成 5~10s 期间持有连接）
    # + 内部独立 session（规则匹配/建图等）叠加下被耗尽（QueuePool 500）；
    # 扩容到 30+20 并缩短等待超时（快速失败优于 30 秒挂起）
    **({"pool_size": 30, "max_overflow": 20, "pool_timeout": 10} if not _is_sqlite else {}),
    **_engine_kwargs,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入: 获取数据库会话"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    """初始化数据库: 创建所有表"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
