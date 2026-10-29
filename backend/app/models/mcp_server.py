"""外部 MCP Server 配置模型（批次F 挂清单 #13 页面化）——管理页增删改查落库

问答链路（tools.py）通过 refresh_mcp_config 加载启用配置到模块缓存，
.stdio 会话缓存按 name 复用——配置变更后由管理接口主动丢弃对应会话。
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base


class McpServer(Base):
    """外部 MCP Server 配置：http（url 流式）或 stdio（本地进程）双 transport"""

    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(50), unique=True)  # 服务名（工具名前缀）
    transport: Mapped[str] = mapped_column(String(10))  # "http" / "stdio"
    # http：流式端点 URL；stdio：启动命令（如 npx）
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    command: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # stdio 参数列表（JSON 数组字符串，如 '["-y", "pkg", "/data"]'）
    args_json: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def to_config(self) -> dict:
        """转 tools.py 消费的配置 dict（与 .env JSON 配置同构）"""
        import json

        if self.transport == "stdio":
            # 质检 P3-3：DB 数据被手改损坏时降级空参数（不 500）
            try:
                args = json.loads(self.args_json) if self.args_json else []
            except json.JSONDecodeError:
                args = []
            return {
                "name": self.name, "transport": "stdio",
                "command": self.command or "", "args": [str(a) for a in args],
            }
        return {
            "name": self.name, "transport": "http", "url": self.url or "",
        }
