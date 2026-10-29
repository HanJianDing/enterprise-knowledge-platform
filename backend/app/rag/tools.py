"""工具调用循环（产品化 B3）：问答流程按需调用工具，结果注入生成上下文

设计取舍：不依赖模型侧原生 function calling 协议，而是"JSON 协议工具循环"——
轻量模型（classifier_model）输出工具决策 JSON，程序负责执行。与 B1 的
extract_calculation 同构：LLM 只决策与提取，执行交给程序（可审计、可控、防注入）。

两类工具：
1. 内置工具（本地执行，零依赖）：calculator（ast 白名单求值）、datetime
2. 外部 MCP Server（settings.mcp_servers，JSON 数组）：用官方 MCP 客户端连接，
   问答时按需调用——"检索制度 + 查实时系统"合成回答的落地通道（如 OA 查报销进度）
"""
import asyncio
import json
import re
from datetime import datetime

from loguru import logger

from app.config import settings
from app.rag.agentic import safe_eval_expression
from app.rag.llm import get_llm

TOOL_PROMPT = """你是工具调度助手。判断用户问题是否需要调用工具，需要则输出工具名与参数。

可用工具（args 必须严格按各工具的"参数"字段名与类型填写）：
{tools_text}

规则：
- 用户问题需要计算、或需要"当前日期/时间/星期"等实时信息时，调用对应工具
- 用户问题明确点名某个工具（如"用 echo 工具"）时，必须调用该工具
- 与任何工具都无关时，needs_tool 为 false，tool 为 null
- args 只填工具所需的参数，参数名必须与工具声明完全一致
- 只输出 JSON，不要输出解释文字

用户问题：
{question}
{feedback_text}
输出格式（严格遵守）：
{{"needs_tool": true/false, "tool": "工具名或null", "args": {{}}}}"""


def format_tools_text(candidate_tools: list[dict]) -> str:
    """工具候选转决策提示词文本（含参数声明）。

    批次F 验收修复（挂清单 #13 端到端验证发现）：此前只给工具描述，
    LLM 猜不出参数名——真实验收：调 echo 工具传空参数，服务器回
    "expected string, received undefined at message"。参数声明进
    提示词后 LLM 按声明生成 args。
    """
    lines = []
    for t in candidate_tools:
        line = f"- {t['name']}：{t['description']}"
        schema = t.get("args_schema")
        if schema:
            line += f"\n  参数：{schema}"
        lines.append(line)
    return "\n".join(lines)


# ── 内置工具注册表 ──

BUILTIN_TOOLS: dict[str, dict] = {
    "calculator": {
        "description": "计算算术表达式（仅支持数字与 + - * / 括号）",
        "args_schema": {"expression": "算术表达式，如 10-3"},
    },
    "datetime": {
        "description": "查询当前日期、时间、星期",
        "args_schema": {},
    },
    "python": {
        # 批次C·11b 代码执行沙箱：受限为默认——仅纯计算类代码
        # （math/statistics/random 模块），多步计算/统计/抽签类问题用
        # 批次F 验收修复：描述强调 print 输出——LLM 生成列表推导
        # 忘了 print（真实验收："帮我抽三个随机数"生成
        # [random.randint(1,100) for _ in range(3)] 无输出，沙箱返回
        # "无输出"导致答不出）
        "description": (
            "执行 Python 代码做计算、统计或随机抽取（沙箱受限：仅支持"
            "纯计算类代码，可用 math/statistics/random 模块；适合求平均/"
            "求和/多步换算/随机抽样等）。代码最后必须用 print() 把结果"
            "打印出来，否则拿不到计算结果"
        ),
        "args_schema": {
            "code": "Python 代码，必须以 print() 输出结果，如 print(sum([1,2,3])/3)",
        },
    },
}


def execute_builtin_tool(name: str, args: dict) -> str | None:
    """执行内置工具；工具不存在返回 None（调用方按失败处理）"""
    if name == "calculator":
        expression = str(args.get("expression", "")).strip()
        result = safe_eval_expression(expression)
        if result is None:
            return f"计算失败：表达式不合法（{expression}）"
        result_text = (
            f"{int(result)}" if isinstance(result, float) and result.is_integer() else str(result)
        )
        return f"{expression} = {result_text}"
    if name == "datetime":
        now = datetime.now()
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {weekdays[now.weekday()]}"
    if name == "python":
        # 批次C·11b：开关关闭时该工具不进注册表（受限为默认、可配置）
        if not settings.agent_sandbox_enabled:
            return "代码执行沙箱已关闭"
        from app.rag.sandbox import execute_sandbox

        code = str(args.get("code", "")).strip()
        if not code:
            return "代码为空"
        return execute_sandbox(code)
    return None


# ── 外部 MCP Server 连接器（配置了才生效）──

# 批次F 验收修复（#13 端到端验证发现）：放弃跨请求会话缓存，改用完即弃。
# 此前缓存 (streams, session) 长连接复用：mcp SDK 的 task group/cancel
# scope 绑定创建它的那次请求 task，请求结束 scope 栈清空，后续请求复用
# 会话即触发 "Attempted to exit a cancel scope that isn't the current
# task's"（真实验收：第二次问答 SSE 500）。每次调用起新进程（npx 约
# 1~2s，工具调用低频可接受），会话生命周期完全在单次请求内，取消
# 作用域天然正确。initialize 失败时对称退出上下文（进程回收）。


async def _get_stdio_session(name: str, cfg: dict):
    """启动 stdio server 并返回 (streams, session)——用完即弃，调用方负责退出"""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client
    from mcp import StdioServerParameters

    streams = None
    session = None
    try:
        params = StdioServerParameters(
            command=cfg["command"], args=cfg.get("args") or []
        )
        streams = stdio_client(params)
        read, write = await streams.__aenter__()
        try:
            session = ClientSession(read, write)
            # 必须先进入 session 上下文（__aenter__ 创建 task group 并启动
            # 接收循环/dispatcher），再 initialize——此前跳过 __aenter__ 直接
            # initialize，必现 "send_raw_request called before run()"
            await session.__aenter__()
            await session.initialize()
        except Exception:
            # 质检 P2-1：session 构造/__aenter__/initialize 任一失败都要
            # 确保 streams 退出（子进程回收）——session 已进入时先对称
            # 退出它；两处退出各自容错，一个失败不阻断另一个
            if session is not None:
                try:
                    await session.__aexit__(None, None, None)
                except Exception as e2:
                    logger.warning("stdio session 退出失败: {} - {}", name, e2)
            await streams.__aexit__(None, None, None)
            raise
        return streams, session
    except asyncio.CancelledError:
        # 补审复核观察1：取消路径收口——客户端断连取消任务时
        # CancelledError 是 BaseException 不被下方 except Exception 捕获，
        # 已进入的 streams 子进程会泄漏；此处回收后继续传播取消
        if streams is not None:
            try:
                await streams.__aexit__(None, None, None)
            except Exception as e2:
                logger.warning("stdio 取消清理失败: {} - {}", name, e2)
        raise
    except Exception as e:
        logger.warning("stdio MCP Server 启动失败: {} - {}", name, e)
        return None


async def _close_stdio_session(streams, session, name: str) -> None:
    """对称退出 stdio 会话上下文（session task group + streams 进程回收）。

    质检 P2-2：拆两个独立 try——session 退出失败不能阻断 streams
    退出（子进程回收），反之亦然
    """
    try:
        await session.__aexit__(None, None, None)
    except Exception as e:
        # 批次C.5 P3-9：清理失败留日志（原裸 except pass 静默吞——
        # 进程回收失败无迹可查）
        logger.warning("stdio 会话清理失败: {} - {}", name, e)
    try:
        await streams.__aexit__(None, None, None)
    except Exception as e:
        logger.warning("stdio 进程清理失败: {} - {}", name, e)


async def _drop_stdio_session(name: str) -> None:
    """兼容保留（旧调用点）：用完即弃模式下无缓存可丢，空操作"""
    return None


async def shutdown_stdio_sessions() -> None:
    """服务停机钩子（兼容保留）：用完即弃模式下无常驻会话需回收"""
    return None

def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON 对象（平衡括号扫描，兼容嵌套对象如 args）。

    正则 [^{}]* 遇到嵌套大括号会提前截断，必须按深度配对截取。
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start:i + 1])
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _parse_mcp_servers() -> list[dict]:
    """解析 MCP Server 配置（页面配置 DB 缓存 + .env 兜底），非法配置忽略。

    批次F 验收修复（挂清单 #13 页面化）：配置主来源改为管理页入库
    （DB 缓存，问答流程开头 refresh_mcp_config 填充），.env 的
    MCP_SERVERS 保留为兜底通道（配置注入/CI）。双 transport：
    - HTTP 流式：{"name": "oa", "url": "http://oa.internal/mcp/"}
    - stdio（本地进程，主流 MCP server 形态）：
      {"name": "files", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]}
    """
    out = list(_db_servers_cache) if _db_servers_cache is not None else []
    raw = (settings.mcp_servers or "").strip()
    if not raw:
        return out
    try:
        servers = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("MCP_SERVERS 配置不是合法 JSON，忽略外部工具")
        return out
    if not isinstance(servers, list):
        logger.warning("MCP_SERVERS 必须是数组格式 [{name, url}] 或 [{name, command, args}]，忽略外部工具")
        return out
    for s in servers:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        name = str(s["name"])
        if s.get("url"):
            out.append({"name": name, "transport": "http", "url": str(s["url"])})
        elif s.get("command"):
            raw_args = s.get("args") or []
            # 批次C 质检 P3：args 为字符串时按字符拆解（"abc"→a/b/c）——
            # 配置写错成字符串时按单参数处理，不静默拆碎
            if isinstance(raw_args, str):
                raw_args = [raw_args]
            out.append({
                "name": name, "transport": "stdio",
                "command": str(s["command"]),
                "args": [str(a) for a in raw_args],
            })
    return out


# ── 页面化配置缓存（批次F 挂清单 #13）──
# DB 为主：管理页增删改查落库，问答流程每问开头 refresh 填充缓存；
# 缓存未填充（如非问答路径/DB 异常）时 .env 兜底（见 _parse_mcp_servers）
_db_servers_cache: list[dict] | None = None
# 工具元数据缓存（质检 P2-3）：用完即弃模式下工具列举若每问都起
# npx 进程，每次问答固定 +1~2s——按配置指纹缓存工具列表，配置变更
# （invalidate_mcp_config）才失效；真实 call_tool 仍用完即弃
_tools_meta_cache: dict[str, list[dict]] = {}
_tools_meta_lock = None  # 惰性 asyncio.Lock（缓存首填并发保护）


def _tools_meta_lock_get():
    global _tools_meta_lock
    if _tools_meta_lock is None:
        _tools_meta_lock = asyncio.Lock()
    return _tools_meta_lock


async def refresh_mcp_config() -> None:
    """从 DB 加载启用的 MCP Server 配置到模块缓存（问答流程开头调用）"""
    global _db_servers_cache
    try:
        from sqlalchemy import select

        from app.infrastructure.database import async_session_factory
        from app.models.mcp_server import McpServer

        async with async_session_factory() as db:
            rows = (await db.execute(
                select(McpServer).where(McpServer.enabled.is_(True))
                .order_by(McpServer.created_at)
            )).scalars().all()
            _db_servers_cache = [r.to_config() for r in rows]
    except Exception as e:
        # 加载失败保持旧缓存（配置不抖动）；无旧缓存时留 None（.env 兜底）
        logger.warning("MCP 页面配置加载失败（沿用旧缓存/env 兜底）: {}", e)


def invalidate_mcp_config() -> None:
    """管理页增删改后置空缓存——下次问答开头重新加载"""
    global _db_servers_cache, _tools_meta_cache
    _db_servers_cache = None
    _tools_meta_cache = {}  # 配置变更：工具元数据缓存一并失效（质检 P2-3）


# ── 危险操作检测（批次F 挂清单 #15）──
# 工具决策前按问题文本识别"借工具/程序能力操作本机资源"类请求，
# 命中直接拒绝（直出拒绝文案），不执行工具、不走 web 兜底——
# 此前此类问题漏到 web 兜底，LLM 反而教学如何读文件（真实验收 6.9）
_DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p in (
        # 质检 P2-3 收紧：不含"执行"——"用 python 执行代码算平均值"是
        # 内置沙箱正面场景（沙箱本身受限，拒绝的是"操作本机资源"意图）
        r"用\s*(?:python|代码|程序|工具|脚本)\s*(?:帮我|给我)?\s*(?:读取|读出|删除|修改|写入)",
        r"(?:帮我|请|我要)?(?:读取|读出|删除|清空|修改)\s*(?:我|本机|本地|电脑|这台机器|服务器|C\s*盘|D\s*盘)[^？?。]{0,10}(?:文件|数据|目录|文件夹|磁盘|内容)",
        r"执行\s*(?:任意|系统|shell|cmd|命令行)\s*命令",
        # 质检 P2-3 收紧：删除类必须显式"整个/全部"——"怎么删除数据库中
        # 的重复记录"是管理操作咨询，不误拒
        r"(?:格式化|清空)\s*(?:整个|全部)?\s*(?:磁盘|硬盘|数据库)",
        r"删除\s*(?:整个|全部)\s*(?:磁盘|硬盘|数据库|文件|数据)",
        # 质检 P2-3 收紧：裸"获取账号"不误拒（"如何获取账号权限"是权限
        # 咨询）；获取类仅限"他人+密码凭据"目标
        r"(?:窃取|盗取|破解)\s*(?:管理员|其他用户|别人|他人)?\s*的?\s*(?:密码|账号|凭据|token)",
        r"获取\s*(?:管理员|其他用户|别人|他人)\s*的?\s*(?:密码|凭据|token)",
    )
]


def detect_dangerous_request(question: str) -> bool:
    """危险操作请求检测（保守白名单式：只匹配明确危险意图，宁漏勿伤）"""
    q = (question or "").strip()
    if not q:
        return False
    return any(p.search(q) for p in _DANGEROUS_PATTERNS)


async def list_external_tools() -> list[dict]:
    """连接所有配置的外部 MCP Server，列出可用工具（问答时供 LLM 决策）。

    返回 [{"server": 服务名, "name": 工具名, "description": 描述,
    "args_schema": 参数声明 JSON}]；单服务连接失败不影响其他服务
    （记日志跳过）。
    批次C·11c：http/stdio 双 transport；批次F 验收修复：stdio 用完即弃
    （会话缓存跨请求复用会触发 mcp SDK cancel scope 异常，见模块注释）。
    质检 P2-3：工具元数据按配置指纹缓存——列举每问起 npx 进程会让
    每次问答固定 +1~2s，配置变更时由 invalidate_mcp_config 失效。
    """
    servers = _parse_mcp_servers()
    fingerprint = json.dumps(
        servers, sort_keys=True, ensure_ascii=False, default=str
    )
    # 补审复核观察3：缓存首填加锁——invalidate 后并发首问可能同时
    # 起多个 npx 进程列举；锁内双检（惰性锁，同 _stdio_session_lock 模式）
    lock = _tools_meta_lock_get()
    async with lock:
        cached = _tools_meta_cache.get(fingerprint)
        if cached is not None:
            return cached
        tools = await _collect_external_tools(servers)
        # 补审复核观察2：至少一个服务列举成功才缓存——全部失败
        # （服务瞬时不可用）时不缓存，下次问答自动重试列举
        if tools:
            _tools_meta_cache[fingerprint] = tools
        return tools


async def _collect_external_tools(servers: list[dict]) -> list[dict]:
    """连接各外部 MCP Server 列举工具（list_external_tools 的列举主体）"""
    import httpx
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.session import ClientSession

    tools: list[dict] = []
    for server_cfg in servers:
        try:
            if server_cfg["transport"] == "stdio":
                got = await _get_stdio_session(server_cfg["name"], server_cfg)
                if got is None:
                    continue
                streams, session = got
                try:
                    result = await session.list_tools()
                finally:
                    await _close_stdio_session(streams, session, server_cfg["name"])
            else:
                async with httpx.AsyncClient(timeout=15) as http:
                    async with streamable_http_client(server_cfg["url"], http_client=http) as streams:
                        async with ClientSession(*streams) as session:
                            await session.initialize()
                            result = await session.list_tools()
            for t in result.tools:
                # 批次F 验收修复：参数声明进决策提示词（此前 LLM 猜参数
                # 传空——echo 回 "expected string, received undefined"）
                schema = getattr(t, "inputSchema", None)
                tools.append({
                    "server": server_cfg["name"],
                    "name": f"{server_cfg['name']}.{t.name}",
                    "description": t.description or "",
                    "args_schema": json.dumps(schema, ensure_ascii=False) if schema else "",
                })
        except Exception as e:
            logger.warning("外部 MCP Server 连接失败（跳过）: {} - {}", server_cfg['name'], e)
    return tools


async def call_external_tool(server_name: str, tool_name: str, args: dict) -> str:
    """调用外部 MCP Server 的工具，返回文本结果；失败返回错误文案（不抛异常）"""
    import httpx
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.session import ClientSession

    server_cfg = next(
        (s for s in _parse_mcp_servers() if s["name"] == server_name), None
    )
    if server_cfg is None:
        return f"外部工具服务 {server_name} 未配置"
    try:
        if server_cfg["transport"] == "stdio":
            got = await _get_stdio_session(server_name, server_cfg)
            if got is None:
                return f"外部工具服务 {server_name} 启动失败"
            streams, session = got
            try:
                result = await session.call_tool(tool_name, args)
            finally:
                await _close_stdio_session(streams, session, server_name)
        else:
            async with httpx.AsyncClient(timeout=15) as http:
                async with streamable_http_client(server_cfg["url"], http_client=http) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, args)
        texts = [
            getattr(item, "text", None)
            for item in (result.content or [])
            if getattr(item, "text", None)
        ]
        return "\n".join(texts) or "（工具无文本输出）"
    except Exception as e:
        logger.warning("外部工具调用失败: {}.{} - {}", server_name, tool_name, e)
        await _drop_stdio_session(server_name)
        return f"外部工具调用失败：{server_name}.{tool_name}"


# ── 决策与执行编排 ──

def should_try_tools(question: str) -> bool:
    """启发式闸门：只有"可能用得上工具"的问题才走检测（省一次 LLM 调用）。

    含数字（计算可能）、问实时日期时间、或配置了外部 MCP Server（能力未知）时开启。
    批次F 验收扩展：中文数字与计算意图词——"帮我抽三个随机数"此前被漏掉
    （"三"不是阿拉伯数字且无外部工具配置时工具决策根本不跑，真实验收：
    沙箱随机抽样没被调用，答"我无法运行代码"走 web 兜底）
    """
    if _parse_mcp_servers():
        return True
    if re.search(r"\d", question):
        return True
    if any(kw in question for kw in ("今天", "几号", "星期", "日期", "时间", "现在几", "周几")):
        return True
    # 质检 P2-4 收窄：中文数字须处量词语境（"三个/五天/十人/两笔"）——
    # 裸字符类会命中"一起/一般/十分/五险一金"等日常高频词，普通问答
    # 每问多一次 classifier LLM 调用；原始漏检场景"抽三个随机数"已由
    # 下方"随机"关键词覆盖，收窄不回退
    if re.search(r"[一二三四五六七八九十百千万两](?:个|次|天|人|笔|张|份|项|名|位|只|台|套|回|遍|层|级|步)", question):
        return True
    return any(kw in question for kw in (
        "计算", "算算", "加起来", "求和", "平均", "随机", "抽签", "抽奖",
        "加减乘除", "等于多少",
    ))


async def detect_tool_call(question: str, candidate_tools: list[dict],
                           feedback: str | None = None) -> dict | None:
    """LLM 决策：是否调用工具、调哪个、参数是什么。

    返回 {"tool": 完整工具名, "args": {...}}；不需要工具/解析失败返回 None。
    feedback（批次F 验收修复）：上次调用失败的错误反馈——参数声明进
    提示词后小模型（qwen-flash）仍概率性传空参数（真实验收：echo 上一轮
    参数正确、下一轮又 -32602），带错误重试一次显著提高遵从率。
    """
    if not candidate_tools:
        return None
    tools_text = format_tools_text(candidate_tools)
    # 质检 P3 遗留修复：feedback 经模板占位符拼在"输出格式"之前——
    # 此前拼在模板末尾（JSON 输出示例之后），模型先看示例后看反馈，
    # 重试遵从率打折
    feedback_text = (
        "上次调用失败：" + feedback
        + "。请严格按上面声明的参数名与类型重新生成 args，不要遗漏必填参数。"
        if feedback else ""
    )
    try:
        llm = get_llm(streaming=False, model=settings.classifier_model)
        # 挂清单收敛：工具决策 LLM 调用走专用池（原在 async 函数内
        # 同步调用直接阻塞事件循环——比挂共享池更严重）
        from app.rag.llm import run_in_llm_executor

        response = await run_in_llm_executor(
            llm.invoke, TOOL_PROMPT.format(
                tools_text=tools_text, question=question,
                feedback_text=feedback_text,
            )
        )
        raw = response.content if hasattr(response, "content") else str(response)
        data = _extract_json(raw)
        if data is None:
            logger.warning("工具决策输出无 JSON: {}", raw[:200])
            return None
        if not data.get("needs_tool") or not data.get("tool"):
            return None
        tool = str(data["tool"]).strip()
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        # 决策出的工具名必须在候选内（防 LLM 幻觉调用任意工具）
        if tool not in {t["name"] for t in candidate_tools}:
            logger.warning("工具决策不在候选列表（忽略）: {}", tool)
            return None
        return {"tool": tool, "args": args}
    except Exception as e:
        logger.warning("工具决策失败（跳过）: {}", e)
        return None


async def execute_tool_call(tool_call: dict) -> str | None:
    """执行工具决策：内置工具本地执行，外部工具走 MCP 客户端。

    返回执行结果文本；工具不存在/失败返回 None（调用方按失败处理）。
    批次C 质检 P2-1：内置工具同步执行（沙箱 subprocess.run 最长阻塞
    5s）——放线程池，不阻塞事件循环（项目 to_thread 惯例）。
    """
    import asyncio

    tool = tool_call["tool"]
    args = tool_call.get("args") or {}
    if tool in BUILTIN_TOOLS:
        return await asyncio.to_thread(execute_builtin_tool, tool, args)
    # 外部 MCP 工具名形如 "服务名.工具名"
    if "." in tool:
        server_name, _, tool_name = tool.partition(".")
        return await call_external_tool(server_name, tool_name, args)
    return None


def format_tool_note(tool: str, result: str) -> str:
    """工具执行结果转注入上下文的说明文本"""
    return f"【工具查询结果】{tool}：{result}（由程序执行，非模型推算）"
