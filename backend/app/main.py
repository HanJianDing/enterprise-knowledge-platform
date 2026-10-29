"""FastAPI 应用入口"""
import asyncio
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.infrastructure.database import init_db
from app.api.v1 import routers
from app.models.user import User
from app.middleware.auth import hash_password


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    # 启动时: 初始化数据库 + 预设管理员 + 配置种子
    logger.info("正在初始化数据库...")
    await init_db()
    await _ensure_admin()
    await _seed_settings()
    await _ensure_default_kb()
    # 阶段6：恢复重启时丢失的上传处理任务（卡在 uploaded/processing 的文档重新入队）；
    # 恢复失败不阻断启动（文档可随后手动重试）
    try:
        from app.services.document_service import recover_processing_documents
        await recover_processing_documents()
    except Exception as e:
        logger.warning(f"上传任务启动恢复失败（不阻断启动，可手动重试）: {e}")
    if settings.sso_test_enabled:
        logger.warning(
            "SSO_TEST_ENABLED=true：测试登录通道已开启（任何人可构造测试身份建号）。"
            "生产环境必须关闭！"
        )
    # 批次2-2：图谱建图任务启动补建（重启丢失口子——以 Neo4j 标记节点为
    # 状态源幂等补齐，图谱关闭/不可用内部静默跳过；不阻塞启动）
    _graph_rebuild_task = None
    if settings.graphrag_enabled:
        try:
            from app.rag.graph import graph_service

            _graph_rebuild_task = asyncio.create_task(
                graph_service.rebuild_missing_graphs()
            )

            def _graph_rebuild_done(task: asyncio.Task) -> None:
                """补建结束取异常防 "Task exception was never retrieved" 噪音"""
                try:
                    exc = task.exception()
                    if exc:
                        logger.warning(f"图谱启动补建异常: {exc}")
                except asyncio.CancelledError:
                    pass

            _graph_rebuild_task.add_done_callback(_graph_rebuild_done)
        except Exception as e:
            logger.warning(f"图谱启动补建任务创建失败（不阻断启动）: {e}")
    # 验收批次1·8g：同义词自动发现后台定时扫描（suggest_auto_enabled 开关）
    _suggest_task = None
    if settings.suggest_auto_enabled:
        from app.services.retrieval_suggest import auto_scan_loop

        _suggest_task = asyncio.create_task(auto_scan_loop())
    logger.info(f"应用启动完成，端口: {settings.backend_port}")
    # MCP 子应用的 lifespan 必须显式启动（FastAPI mount 不会自动跑子应用生命周期）：
    # 其会话管理依赖 anyio task group，不启动会报 "Task group is not initialized"
    if _mcp_asgi is not None:
        async with _mcp_asgi.router.lifespan_context(_mcp_asgi):
            yield
    else:
        yield
    # 关闭时
    if _suggest_task is not None:
        _suggest_task.cancel()
        try:
            await _suggest_task
        except asyncio.CancelledError:
            pass  # 关闭竞态下静默收尾，避免 "Task was destroyed but it is pending" 告警
    if _graph_rebuild_task is not None:
        _graph_rebuild_task.cancel()
        try:
            await _graph_rebuild_task
        except asyncio.CancelledError:
            pass
    # 批次C.5 P3-9：停机回收 stdio MCP 子进程（不留孤儿 npx 进程）
    try:
        from app.rag.tools import shutdown_stdio_sessions

        await shutdown_stdio_sessions()
    except Exception as e:
        logger.warning(f"stdio MCP 会话停机清理失败（不阻断关闭）: {e}")
    logger.info("应用关闭")


async def _ensure_admin():
    """确保管理员账号存在（首次创建时密码取自环境变量，不再硬编码）"""
    from app.infrastructure.database import async_session_factory
    from sqlalchemy import select

    async with async_session_factory() as db:
        result = await db.execute(
            select(User).where(User.username == "admin")
        )
        admin = result.scalar_one_or_none()

        if not admin:
            # 安全修复：硬编码 admin/123456 已被泄露为公开知识；
            # 首次创建必须显式提供初始密码，否则拒绝启动
            password = settings.initial_admin_password
            if not password:
                raise RuntimeError(
                    "首次启动需要创建默认管理员，但 INITIAL_ADMIN_PASSWORD 未在 .env 中配置。"
                    "请在 .env 中设置一个强密码后重启。"
                )
            admin = User(
                username="admin",
                email="admin@example.com",
                hashed_password=hash_password(password),
                role="admin",
            )
            db.add(admin)
            await db.commit()
            logger.info("管理员账号已创建（初始密码来自 INITIAL_ADMIN_PASSWORD，请登录后尽快修改）")
        else:
            logger.info("管理员账号已存在")


async def _seed_settings():
    """确保 system_settings 默认键存在（欢迎页快捷问题）"""
    from app.infrastructure.database import async_session_factory
    from app.services.setting_service import seed_default_settings

    async with async_session_factory() as db:
        await seed_default_settings(db)
        await db.commit()


async def _ensure_default_kb():
    """确保至少存在一个知识库（企业知识库阶段1：全新库启动时创建默认库）。

    默认库设为公共库（is_public=True）：延续改造前"全员共享一个库"的行为，
    老数据迁移（alembic 回填）与新环境首跑都落在它身上；管理员后续可改。
    """
    from app.infrastructure.database import async_session_factory
    from sqlalchemy import select
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User

    async with async_session_factory() as db:
        result = await db.execute(select(KnowledgeBase.id).limit(1))
        if result.scalar_one_or_none():
            return  # 已有知识库（含迁移回填的默认库），不动

        admin_result = await db.execute(
            select(User).where(User.role == "admin").limit(1)
        )
        admin = admin_result.scalar_one_or_none()
        if not admin:
            return  # 管理员尚未创建（理论上不会发生），下次启动再补

        db.add(KnowledgeBase(
            name="默认知识库",
            description="系统自动创建的默认知识库（全员可读），可重命名或设为私有后按需授权",
            is_public=True,
            created_by=admin.id,
        ))
        await db.commit()
        logger.info("已创建默认知识库（is_public=True）")


# 创建应用
app = FastAPI(
    title="企业知识库问答系统",
    description="基于 LangChain + 阿里云百炼 的企业内部知识库问答系统",
    version="1.0.0",
    lifespan=lifespan,
)

# 批次4 可观测：OTEL tracing 必须在 app 创建后立即初始化——
# 在 lifespan（startup 阶段）里挂 FastAPIInstrumentor 对 uvicorn 无效
# （实测请求 span 不产生、trace_id 恒 null；TestClient 场景能掩盖此问题）
from app.observability import init_tracing

init_tracing(app)

# CORS 配置（上线安全修复：放行来源改为 .env 的 CORS_ORIGINS 可配置，
# 逗号分隔；部署时填入真实域名，如 https://shop.example.com）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
for router in routers:
    app.include_router(router)

# MCP Server（产品化 B2）：知识库检索以 MCP 标准协议对外暴露。
# 任何 MCP 客户端（Claude Desktop / Cursor / 企业其他 AI 应用）带本系统
# JWT 即可调用——权限穿透 + 审计留痕，检索范围 fail-closed 限定可见文档。
# 注意：lifespan 函数里显式启动其子应用生命周期（见上方注释）。
_mcp_asgi = None
if settings.mcp_enabled:
    from app.mcp_server import build_mcp_server

    _mcp_server = build_mcp_server()
    # 内层 streamable_http_path 默认 "/mcp"，外层再 mount 到 /mcp 会变成 /mcp/mcp——
    # 内层设 "/" 后端点即 /mcp/（客户端请求 /mcp 会收到 307，httpx 系客户端默认跟随）
    _mcp_asgi = _mcp_server.streamable_http_app(streamable_http_path="/")
    app.mount("/mcp", _mcp_asgi)
    logger.info("MCP Server 已挂载: /mcp")


# 全局异常处理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"未处理的异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请联系管理员"},
    )


@app.get("/")
async def root():
    return {"message": "RAG知识库问答系统 API", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    """健康检查（产品化 C4 升级）：探测数据库与向量库连通性。

    返回 {"status": "ok"|"degraded", "checks": {...}}，供
    scripts/health_check.py 拨测与告警使用（原实现只回"healthy"，
    服务挂着但 DB 挂了也报健康，等于没有检查）。
    """
    import asyncio

    from sqlalchemy import text

    from app.infrastructure.database import async_session_factory
    from app.rag.retriever import get_store_stats

    checks = {"database": False, "vector_store": False}
    try:
        async with async_session_factory() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as e:
        logger.warning(f"健康检查-数据库异常: {e}")
    try:
        await asyncio.to_thread(get_store_stats)
        checks["vector_store"] = True
    except Exception as e:
        logger.warning(f"健康检查-向量库异常: {e}")
    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.backend_port,
        reload=True,
    )
