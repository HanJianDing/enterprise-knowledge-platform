"""MCP Server（产品化 B2）：把知识库检索以 MCP 标准协议暴露给任何 AI 应用

设计要点：
1. 权限穿透——MCP 调用带本系统 JWT（Authorization: Bearer xxx），TokenVerifier
   解出用户后，检索范围 fail-closed 限定在该用户可见文档（复用 kb_service 权限体系）。
   市面上大部分 MCP 集成"工具能调就能用"，这里是部门级权限穿透 + 审计留痕。
2. 返回"自带质量"——生成侧兜底（引用要求/上下文拼接）下沉到检索侧：
   返回的文本含 [来源i] 编号、文件名、片段与引用指令，客户端模型照答即可。
3. 每次工具调用写审计日志（与业务同会话直写，随事务提交）。
"""
from loguru import logger

from app.config import settings
from app.middleware.auth import decode_token

# mcp 2.x：FastMCP 更名为 MCPServer
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.auth.provider import AccessToken, TokenVerifier

MCP_SERVER_NAME = "enterprise-knowledge-base"


class JwtTokenVerifier(TokenVerifier):
    """MCP 调用身份验证：登录 JWT + 长期接入凭证（批次D）双通道。

    - 登录 JWT：复用 decode_token（含过期/签名校验），sv 随 claims 传递，
      _resolve_user 与业务 API 同等地做顶号校验
    - 接入凭证（mcpk_ 前缀）：查库匹配哈希、校验未撤销，绑定账号权限生效；
      凭证不受顶号踢出影响（独立生命周期，撤销即失效）
    AccessToken.subject 承载用户声明、claims 承载 user_id/role 等——工具侧
    据此查库拿用户对象，再走 kb_service 的权限体系。
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        # 通道一：接入凭证（前缀 mcpk_，随机 192 位熵，库里只存 sha256）
        if token.startswith("mcpk_"):
            return await self._verify_credential(token)
        # 通道二：登录 JWT
        try:
            payload = decode_token(token)
        except Exception as e:
            logger.warning(f"MCP token 校验失败: {e}")
            return None
        if payload.get("type") != "access":
            return None
        return AccessToken(
            token=token,
            client_id=str(payload.get("sub") or ""),
            scopes=[str(payload.get("role") or "user")],
            subject=str(payload.get("sub") or ""),
            claims={
                "user_id": str(payload.get("sub") or ""),
                "role": payload.get("role"),
                # sv 随 claims 传递：_resolve_user 需与业务 API 同等地做顶号校验
                "sv": payload.get("sv"),
            },
        )

    async def _verify_credential(self, token: str) -> AccessToken | None:
        """接入凭证校验：哈希查库 + 撤销检查 + 解析绑定账号"""
        import hashlib

        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.mcp_credential import McpCredential
        from app.models.user import User

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        try:
            async with async_session_factory() as db:
                result = await db.execute(
                    select(McpCredential).where(McpCredential.token_hash == token_hash)
                )
                cred = result.scalar_one_or_none()
                if cred is None or cred.revoked:
                    return None
                owner = (await db.execute(
                    select(User).where(User.id == cred.owner_user_id)
                )).scalar_one_or_none()
                if owner is None or not owner.is_active:
                    return None
                return AccessToken(
                    token=token,
                    client_id=str(owner.id),
                    scopes=[str(owner.role or "user")],
                    subject=str(owner.id),
                    claims={
                        "user_id": str(owner.id),
                        "role": owner.role,
                        # 凭证模式标记：_resolve_user 跳过 sv 顶号校验
                        "credential": True,
                        "kb_restrict": cred.kb_ids,
                    },
                )
        except Exception as e:
            logger.warning(f"接入凭证校验失败: {e}")
            return None


def build_mcp_server() -> MCPServer:
    """构建 MCP Server：注册 search_kb / list_visible_kbs 两个工具"""
    from mcp.server.auth.settings import AuthSettings

    server = MCPServer(
        name=MCP_SERVER_NAME,
        title="企业知识库",
        description="企业内部知识库检索服务（部门级权限隔离，检索结果自带引用来源）",
        version="1.0.0",
        token_verifier=JwtTokenVerifier(),
        # token_verifier 必须伴随 AuthSettings（issuer_url 用于 OAuth 元数据广告；
        # 本系统用自有 JWT 直验，不走 OAuth 授权流，此处仅为满足 SDK 契约）
        auth=AuthSettings(
            issuer_url=settings.frontend_base_url or "http://localhost:5173",
            resource_server_url="http://localhost:8000",
            # 本系统 JWT 无 RFC 8707 resource 声明，由 JwtTokenVerifier 自行完成
            # 签发者校验；关闭 SDK 的资源比对（否则 3.0 默认开启会误拒）
            validate_token_resource=False,
        ),
    )

    @server.tool()
    async def search_kb(ctx: Context, question: str, top_k: int = 6) -> str:
        """检索企业知识库，返回与问题相关的文档片段（带 [来源N] 编号与出处）。

        Args:
            question: 检索问题（关键词丰富效果更好）
            top_k: 返回片段条数上限（1~10，默认 6）
        """
        from app.services import mcp_service

        user, kb_restrict = await _resolve_user(ctx)
        if user is None:
            return "【错误】未认证或身份无效：请携带有效的 Authorization: Bearer <token> 调用本工具。"
        return await mcp_service.search_for_user(user, question, top_k, kb_restrict=kb_restrict)

    @server.tool()
    async def list_visible_kbs(ctx: Context) -> str:
        """列出当前用户有权限访问的知识库（名称 + 权限级别）。"""
        from app.services import mcp_service

        user, kb_restrict = await _resolve_user(ctx)
        if user is None:
            return "【错误】未认证或身份无效：请携带有效的 Authorization: Bearer <token> 调用本工具。"
        return await mcp_service.list_visible_kbs_for_user(user, kb_restrict=kb_restrict)

    return server


async def _resolve_user(ctx) -> tuple[object | None, list[str] | None]:
    """从 MCP 请求上下文解析当前用户与可见库限制（TokenVerifier 已校验）。

    返回 (user, kb_restrict)：kb_restrict 为接入凭证的库白名单
    （None=不限，按绑定账号权限兜底）。权限 fail-closed：拿不到身份
    一律返回 (None, None)，绝不允许匿名调用落到检索上。
    """
    try:
        request = ctx.request_context.request
        # Starlette 认证结果挂在 request.user 上（request.auth 只有 scope）；
        # bearer 中间件把 AccessToken 塞进 AuthenticatedUser.access_token
        user_obj = getattr(request, "user", None)
        access_token: AccessToken | None = getattr(user_obj, "access_token", None)
        if access_token is None or not access_token.claims:
            return None, None
        user_id = access_token.claims.get("user_id")
        if not user_id:
            return None, None
        kb_restrict = access_token.claims.get("kb_restrict")
        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.user import User

        async with async_session_factory() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user is None or not user.is_active:
                return None, None
            if not access_token.claims.get("credential"):
                # 顶号校验（与业务 API get_current_user 同语义，fail-closed）：
                # sv 缺失或被其他设备顶号后的旧 token 一律拒绝。
                # 接入凭证模式跳过——凭证有独立生命周期（撤销即失效），
                # 管理员的会话版本变化不应牵连集成凭证
                sv = access_token.claims.get("sv")
                if sv is None or sv != user.session_version:
                    return None, None
            return user, kb_restrict
    except Exception as e:
        logger.warning(f"MCP 身份解析失败（fail-closed 拒绝）: {e}")
        return None, None
