"""测试配置 - 共享 Fixtures"""
import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# 设置测试用的数据库：共享内存 SQLite（StaticPool 单连接串行化，
# 后台任务与会话之间不存在锁竞争；无磁盘文件残留）
import os
os.environ["DATABASE_URL"] = (
    "sqlite+aiosqlite:///file:kb_test_db?mode=memory&cache=shared&uri=true"
)
# 关闭限频（内存计数是进程级全局状态，用例间会互相污染；限频逻辑有专门测试文件单测）
os.environ["RATE_LIMIT_ENABLED"] = "false"
# 测试隔离：向量库与上传目录指向测试专用绝对路径，
# 防止测试污染真实知识库（历史 bug：测试从 backend/ 启动时把数据写进了 backend/backend/ 嵌套目录）
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ["CHROMA_PERSIST_DIR"] = os.path.join(_TESTS_DIR, "test_chroma_db")
os.environ["UPLOAD_DIR"] = os.path.join(_TESTS_DIR, "test_uploads")

from app.main import app
from app.infrastructure.database import init_db, engine, Base

@pytest.fixture(scope="session")
def event_loop():
    """创建 session 级别的事件循环"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """每次测试前重建数据库。

    防御性重试：审计/上传等后台任务与下一个测试的建表可能交错执行，
    撞出瞬时的 lock/表残留时报错重来一次（等后台任务落库完）。
    """
    from sqlalchemy.exc import OperationalError

    async def _reset() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    try:
        await _reset()
    except OperationalError:
        await asyncio.sleep(0.5)
        await _reset()
    yield
    # 收尾前先把上传处理的后台任务取消——
    # 任务若跨用例存活，会占住 SQLite 会话与文件句柄，与下一个用例的
    # drop_all/create_all 抢锁，造成连锁错误。只取消不 gather 等待
    # （被取消的任务若正卡在锁等待里，等它退出可能非常久，直接把收尾挂死）。
    # 审计日志已改为与业务同会话直写（无后台任务），不存在此问题。
    import app.services.document_service as ds

    for t in list(ds._background_tasks):
        t.cancel()
    ds._background_tasks.clear()
    ds._in_flight_doc_ids.clear()
    await asyncio.sleep(0.05)  # 给取消一个生效的机会，不等完成

    # 清理
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    # 关掉连接池：共享内存库随连接销毁；下一个用例拿到全新连接+全新库，
    # 任何残留会话/事务/后台任务状态都无法跨用例存活（彻底隔离）
    await engine.dispose()


@pytest.fixture(autouse=True)
def fake_document_processing(monkeypatch):
    """上传触发的后台处理任务不真跑（嵌入/向量库/网络），立即标记 completed(1 分块)。

    真实处理会调用百炼嵌入 API（测试无密钥、有网络延迟），任务会跨测试存活，
    与下一个测试的 drop_all/create_all 抢 SQLite 锁，造成大面积的
    "database is locked / table already exists" 连锁错误。
    处理管道的真实逻辑由 test_pipeline_* 单独覆盖。
    """
    import app.services.document_service as ds

    monkeypatch.setattr(ds, "process_document", lambda *a, **k: (1, None))
    # 清理旧分块也跳过（无真实写入就没有旧分块；同时避免任务打开真实 Chroma）
    monkeypatch.setattr(ds, "delete_document_from_store", lambda *a, **k: 0)


@pytest_asyncio.fixture
async def client():
    """Async test client"""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def create_user_in_db(
    username: str,
    email: str,
    password: str = "123456",
    role: str = "user",
    must_change_password: bool = False,
) -> None:
    """测试辅助：直接落库创建用户（企业改造阶段3：开放注册接口已关闭）。

    用法：await create_user_in_db("name", "name@test.com")
    """
    from app.middleware.auth import hash_password
    from app.models.user import User
    from app.infrastructure.database import async_session_factory
    async with async_session_factory() as db:
        db.add(User(
            username=username, email=email,
            hashed_password=hash_password(password), role=role,
            must_change_password=must_change_password,
        ))
        await db.commit()


@pytest_asyncio.fixture
async def admin_token(client):
    """获取 admin 用户的 JWT Token"""
    # 直接落库创建 admin（阶段3：注册接口已关闭）
    await create_user_in_db("admin_fixture", "admin_fixture@test.com", role="admin")

    # 登录
    response = await client.post("/api/v1/auth/login", json={
        "username": "admin_fixture", "password": "123456"
    })
    return response.json()["access_token"]


@pytest_asyncio.fixture
async def admin_client(client, admin_token):
    """带 admin 认证的客户端"""
    client.headers["Authorization"] = f"Bearer {admin_token}"
    yield client
    client.headers.pop("Authorization", None)


@pytest_asyncio.fixture
async def user_token(client):
    """获取普通 user 的 JWT Token（不提升角色）"""
    await create_user_in_db("user_fixture", "user_fixture@test.com")
    response = await client.post("/api/v1/auth/login", json={
        "username": "user_fixture", "password": "123456"
    })
    return response.json()["access_token"]


@pytest_asyncio.fixture
async def user_client(client, user_token):
    """带普通用户认证的客户端"""
    client.headers["Authorization"] = f"Bearer {user_token}"
    yield client
    client.headers.pop("Authorization", None)


@pytest_asyncio.fixture
async def default_kb(client):
    """默认知识库（is_public=True 全员可读），返回 kb_id。

    企业知识库阶段1：文档/分类都有库归属；该夹具给 admin 测试提供上传目标，
    给普通用户测试提供"公共库可见"的基础环境。
    不依赖 admin_token 夹具：没有管理员时自建一个（测试环境 DB 每次重建）。
    """
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User
    from app.middleware.auth import hash_password
    from sqlalchemy import select
    from app.infrastructure.database import async_session_factory
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.role == "admin").limit(1))
        admin = result.scalar_one_or_none()
        if not admin:
            admin = User(
                username="kb_admin_fixture", email="kb_admin_fixture@test.com",
                hashed_password=hash_password("123456"), role="admin",
            )
            db.add(admin)
            await db.flush()
        kb = KnowledgeBase(
            name="测试知识库", description="测试用公共库", is_public=True,
            created_by=admin.id,
        )
        db.add(kb)
        await db.commit()
        await db.refresh(kb)
        return kb.id


@pytest_asyncio.fixture
async def private_kb(client, default_kb):
    """私有知识库（is_public=False，无任何授权），返回 kb_id。权限测试用。"""
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User
    from sqlalchemy import select
    from app.infrastructure.database import async_session_factory
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.role == "admin").limit(1))
        admin = result.scalar_one()
        kb = KnowledgeBase(
            name="私有测试库", description="测试用私有库", is_public=False,
            created_by=admin.id,
        )
        db.add(kb)
        await db.commit()
        await db.refresh(kb)
        return kb.id
