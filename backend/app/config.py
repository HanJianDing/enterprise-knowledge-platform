"""应用配置 - 使用 Pydantic Settings 加载环境变量"""
import os
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

# 仓库根目录（backend/app/config.py 向上三级），用于把相对路径锚定为绝对路径。
# 修复"从不同目录启动导致向量库/上传文件分裂成两份"的 CWD 依赖 bug。
REPO_ROOT = Path(__file__).resolve().parents[2]


# 自动查找项目根目录的 .env 文件
def _find_env_file() -> str | None:
    """从当前目录向上查找 .env 文件"""
    candidates = [
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
        REPO_ROOT / ".env",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


class Settings(BaseSettings):
    # 阿里云百炼
    dashscope_api_key: str = ""

    # DeepSeek（裁判模型 LLM-as-judge：外校老师判卷，避免同模型自评偏松；独立账号独立计费）
    deepseek_api_key: str = ""

    # LLM
    llm_model: str = "qwen-plus"
    # 分类/意图澄清用轻量快速模型（延迟优化 D：生成回答仍用 llm_model）
    # 2026-10-10 下线迁移：qwen-turbo 已下线（公告#118177）→ qwen-flash
    classifier_model: str = "qwen-flash"
    llm_temperature: float = 0.1
    # 员工端回答字数上限（企业知识库改造阶段2：从客服时代写死 180 改为可配置）
    user_answer_max_chars: int = 300
    # MCP Server 开关（产品化 B2：知识库检索以 MCP 协议对外暴露，/mcp 端点）
    mcp_enabled: bool = True
    # 外部 MCP Server 列表（产品化 B3：问答流程按需调用外部工具），JSON 数组：
    # [{"name": "oa", "url": "http://oa.internal/mcp/"}]
    mcp_servers: str = ""

    # Embedding
    embedding_model: str = "text-embedding-v2"

    # JWT
    # 安全修复：默认值不再允许为空串（空串签名 = 可伪造任意身份 token）
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # 数据库（必须显式配置；旧的硬编码默认值含生产密码，已移除并需要轮换）
    database_url: str = ""

    # 首次启动时创建默认 admin 的初始密码（仅 admin 不存在时使用；admin 已存在则忽略）
    initial_admin_password: str = ""

    # ChromaDB
    chroma_persist_dir: str = "./backend/chroma_db"

    # 文件上传
    upload_dir: str = "./backend/uploads"
    max_upload_size: int = 20 * 1024 * 1024  # 20MB

    # 服务端口
    backend_port: int = 8000
    frontend_port: int = 5173

    # 限频开关（上线安全修复：登录 5/分、注册 3/分、对话 10/分，按 IP；
    # 测试环境 conftest 置 false，避免用例间全局计数互相干扰）
    rate_limit_enabled: bool = True
    # 登录限频阈值（次/分钟/IP）。批次5.5 压测修复曾把写死的 5 提到 30
    # （perf01~20 压测账号并行登录被 5 误伤）；2026-10-13 用户拍板验收期
    # 恢复 5——压测已结束，防爆破从严，值可配置不再写死
    login_rate_limit_per_min: int = Field(default=5, ge=1, le=600)

    # CORS 放行来源（逗号分隔；部署时填入真实域名，如 https://shop.example.com）
    cors_origins: str = "http://localhost:5173,http://localhost:8501"

    # ── SSO 登录插槽（企业知识库改造 阶段4；不配置即不启用，密码登录不受影响）──
    # 测试 provider：仅本地/联调用（SSO_TEST_ENABLED=true 时登录页出现"测试扫码登录"）
    sso_test_enabled: bool = False
    # 企业微信网页授权（三项齐全才启用；回调域名需已备案 + HTTPS）
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_secret: str = ""
    # 前端地址（SSO 回调 302 的目标 + provider 回调拼接），部署时设为真实域名
    frontend_base_url: str = "http://localhost:5173"
    # ── 企微对话通道（批次E·6e；等企微账号启用后填配置即开）──
    # token/aes_key 未配置 = 通道关闭（回调端点 503）；test_mode 跳过
    # 签名校验（本地联调用，生产必须关）
    wecom_channel_token: str = ""
    wecom_channel_aes_key: str = ""
    wecom_channel_test_mode: bool = False

    # 检索提准（验收批次1·1b）：文档引用图 PageRank 重要性信号加成系数。
    # 批次A·⑩ 三档实测定案（0/0.1/0.3，raw+rerank 双口径，96 题全层+8 题权威层）：
    # - α=0.1 最优：raw 口径 MRR 0.841 三档最高（α=0 为 0.832、α=0.3 为 0.824——
    #   过度加成伤害无关题）；rerank 口径 hit 98.96% 与 α=0.3 持平
    # - 权威题层 rerank 口径三档无差（100%/0.667）；raw 口径 MRR 随 α 下降
    #   （0.604→0.542→0.521）——期望文档 pagerank 普遍不高（考勤 0.34/报销 0.34），
    #   引用密度低根因，语料增强见 8h 清单项
    # 结论：α=0.1 常开（零副作用+真收益）；.env 可调/可关
    retrieval_pagerank_alpha: float = 0.1

    # 父子双层分块开关（验收批次1·1c，激活实验后默认开）：不再全局开关——
    # 仅对厚文档（>3000 字）自动启用子分块（厚文档才是语义稀释重灾区，
    # 600 字级文档切块只添噪音）；厚文档细节题上验证收益后定案。
    # 保留此开关作为总闸（.env 可关）
    retrieval_subchunk_enabled: bool = True
    # 子分块启用阈值：文档超过该字数才切子块（自动点火条件，无需手工干预）
    retrieval_subchunk_min_chars: int = 3000
    # 同义词自动发现（验收批次1·8g）：后台定时扫描开关与间隔（小时，≥1 防热循环烧 LLM）
    suggest_auto_enabled: bool = True
    suggest_scan_interval_hours: int = 24

    # ── RAPTOR 摘要树（批次11a）──
    # 聚类摘要树总开关；厚文档（>raptor_min_chars）处理完成后自动建树
    # （特征自动点火，用户原则：功能必须真的会点火，不许"建了关着"）；
    # 建树失败只留日志，文档正常可用（降级）
    raptor_enabled: bool = True
    # 建树阈值：文档超过该字数才建摘要树（短文档语义不稀释，建树只添成本）
    raptor_min_chars: int = 5000
    # 摘要树层数上限（层 0=叶子块；35k 字文档 3 层足够）
    raptor_max_levels: int = 3
    # RRF 融合时摘要块的固定加成（与 doc_summary 目录块 ×1.5 同款先例——
    # 长摘要块在 BM25 长度归一化下吃亏，固定加成保证宏观问题可召回）
    raptor_summary_boost: float = 1.5
    # 批次B：树遍历检索腿开关（RAPTOR 论文第二种模式——从树顶逐层下钻
    # 选最相关簇；与折叠树摘要块平铺互补，作为 RRF 第三条腿恒参与
    # 有树文档的检索，无需问法判定）
    raptor_traversal_enabled: bool = True
    # 目录摘要块（RAPTOR-lite）生成开关：批次11a 对比档③"纯 RAPTOR"时关闭
    toc_summary_enabled: bool = True

    # ── 知识图谱检索（验收批次2）──
    # 建图/查询总开关；Neo4j 连接信息（本地 zip 版，随用随启 + 降级兜底：
    # 连接失败自动回退纯向量检索，见 graph/query.py）
    graphrag_enabled: bool = True
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    # 图隔离租户标签（批次2-2 方案C）：演示/真实两环境共用同一 Neo4j 实例时，
    # 节点按 env 属性隔离（.env.demo 配 demo、真实环境配 real）；
    # 密码为空 = 图谱关闭，此标签不参与（真实环境默认降级纯向量）
    neo4j_env: str = "demo"

    # 批次5 A1：非流式 LLM 调用超时（分类/消歧/抽取等）——超时放弃等待
    # 走降级路径，防上游接口挂起拖垮请求；流式生成不在此限
    llm_timeout_seconds: float = 120.0
    # ── 批次C·11b Agent 能力 ──
    # 反思循环开关：编排类问题（拆解/对比/汇总）生成后自检，
    # 不合格带修正意见重生成（次数上限见下）；自检失败静默跳过
    # （不阻塞主链路）。普通单问不做反思（每问省一次 LLM 调用）
    agent_reflection_enabled: bool = True
    # 批次C.5：重试次数可配置——默认 1 次（保守：成本与延迟可控），
    # 复杂场景可调到 2~4（每轮重试后重新自检，通过即停）
    agent_reflection_max_retries: int = 1
    # 代码执行沙箱开关：受限为默认——"python"工具仅允许纯计算类代码
    # （subprocess 隔离+超时+无网络+输出截断）；.env 可关
    agent_sandbox_enabled: bool = True
    # 批次D·3a：扫描件 OCR 的页数上限（超过只识别前 N 页，防超大文件拖垮处理）
    ocr_max_pages: int = 20
    # ── 批次D 后续：vision provider 抽象（数据不出域预留）──
    # dashscope=百炼云 qwen3-vl（默认）；openai_compat=本地多模态端点
    # （内网 vLLM 跑 qwen-vl 系列，配 VISION_API_BASE 即切换——图片
    # 不再上传外部临时存储）。用户拍板：代码预留、部署触发制
    vision_provider: str = "dashscope"
    vision_api_base: str = ""
    vision_api_key: str = ""  # 空=回退 llm_api_key/dashscope_api_key
    vision_model: str = "qwen3-vl-plus"
    # 批次5 7a：并发文档处理上限（批量导入时同时处理任务数，
    # 保护 LLM/嵌入 API 不被打爆；seed 脚本"逐份等待"的手工版升级为机制）
    concurrent_upload_limit: int = 3

    # ── 可观测（批次4）──
    # otel_enabled=False 或 OTEL 包缺失时全部静默降级（不影响问答主链路）
    otel_enabled: bool = False
    # console=本地开发（trace 打到日志）；otlp=上报 OTEL Collector（Jaeger/ClickHouse）
    otel_exporter: str = "console"
    # OTLP HTTP 端点（docker-compose 的 otel-collector 4318 端口）
    otel_endpoint: str = "http://localhost:4318/v1/traces"
    # 服务名（批次5.5：Jaeger UI 按服务名筛选，此前默认 unknown_service）
    otel_service_name: str = "rag-knowledge-base"

    @field_validator("suggest_scan_interval_hours")
    @classmethod
    def _suggest_interval_min_one(cls, v: int) -> int:
        """质检低危修复：误配 0/负数会导致后台任务热循环调用 LLM"""
        return max(1, v)

    @field_validator("concurrent_upload_limit")
    @classmethod
    def _concurrent_limit_min_one(cls, v: int) -> int:
        """批次5 质检低#6：误配 0 会让信号量永久排队（所有文档卡 processing）"""
        return max(1, v)

    @field_validator("agent_reflection_max_retries")
    @classmethod
    def _reflection_retries_clamped(cls, v: int) -> int:
        """批次C.5：重试次数钳制 0~4——误配大数（如 100）会每问烧
        上百次 LLM 调用（成本失控）；负数按 0（不做反思）处理"""
        return max(0, min(4, v))

    # ── 私有化预留（企业知识库改造 阶段6；不配置即走百炼云 API）──
    # LLM_API_BASE 指向 OpenAI 兼容端点（如内网 vLLM）时，生成改用该端点
    llm_api_base: str = ""
    llm_api_key: str = ""
    # rerank 端点覆盖（内网自建重排服务时使用）
    rerank_api_base: str = ""
    # ── Web 搜索兜底（批次A·11f）──
    # 知识库检索无结果时的外部搜索兜底。真实企业内网默认关闭
    # （数据不出域原则）；演示环境配博查 key 即开（免费额度够演示）
    web_search_enabled: bool = False
    web_search_api_key: str = ""
    # 低相关兜底阈值（批次语料增强二期）：rerank top1 分数低于该值时视为
    # "知识库答不了"触发 web 兜底——检索层无分数过滤，弱相关问题也会返回
    # 满额结果，仅"无结果"判定会让兜底永远点不了火（E2E 实测发现）。
    # 0.15 为实测标定（2026-10-13 演示库）：正常题 top1 0.206~0.838，
    # 知识库外题 0.006~0.128，间隙干净；规则/图谱腿的复杂度类型
    # （approval/relation）不参与判定（确定性通道，检索弱不代表答不了）。
    # 质检 P3：范围钳制 [0,1]——误配负值/大于 1 会反转判定语义
    web_search_min_score: float = Field(default=0.15, ge=0.0, le=1.0)
    # 批次F 验收修复：L1/L2 分类过滤检索的相关性扩搜阈值——分类内 rerank
    # top1 低于该值时触发 L3 全库兜底（验收实测：分类过滤把更高相关的
    # 跨分类文档排除在外，L1 top1=0.628 而全库 top1=0.785，答案答成
    # 另一份文档；0.7 为实测标定：正常分类内命中 ≥0.7，需扩搜场景
    # 0.62~0.68，间隙干净）。仅大库逐级下钻路径生效（小库快路是全库
    # 检索后置挑拣，本就不排除文档）
    l1_expand_min_score: float = Field(default=0.7, ge=0.0, le=1.0)
    # 批次F 前置小批（质检 P2 缺口）：每分钟调用上限——低相关判定扩宽
    # 触发面后，弱相关问题也出网，兜一层进程级限速防连锁误触发烧额度
    # （限速触发静默返回空，走"知识库无结果"提示路径）
    web_search_rate_per_min: int = Field(default=20, ge=1, le=600)

    # 重排 provider（验收批次1·1e）：dashscope=百炼云 / openai_compat=OpenAI 兼容端点
    rerank_provider: str = "dashscope"
    # 重排模型名（可配；dashscope 默认 gte-rerank-v2——实测 MRR 0.902 优于
    # qwen3-rerank 0.888；gte-rerank-v2 正式下线后改 qwen3-rerank；
    # openai_compat 按部署模型填）
    rerank_model: str = "gte-rerank-v2"

    model_config = {
        "env_file": _find_env_file() or ".env",
        "env_file_encoding": "utf-8",
    }

    @field_validator("jwt_secret_key")
    @classmethod
    def _jwt_secret_must_not_be_empty(cls, v: str) -> str:
        """启动即校验：JWT 密钥为空时拒绝启动，防止用空串签名 token（可伪造）。"""
        if not v or not v.strip():
            raise ValueError(
                "JWT_SECRET_KEY 未配置：请在 .env 中设置一个足够长的随机密钥"
                "（如 python -c \"import secrets; print(secrets.token_urlsafe(48))\"）"
            )
        return v

    @field_validator("database_url")
    @classmethod
    def _database_url_must_not_be_empty(cls, v: str) -> str:
        """启动即校验：数据库连接串必须显式配置。"""
        if not v or not v.strip():
            raise ValueError(
                "DATABASE_URL 未配置：请在 .env 中设置数据库连接串"
            )
        return v

    @model_validator(mode="after")
    def _resolve_relative_paths(self) -> "Settings":
        """把相对路径锚定到仓库根目录，消除 CWD 依赖。

        修复前：`./backend/chroma_db` 从仓库根启动 → backend/chroma_db，
        从 backend/ 启动 → backend/backend/chroma_db，向量库被拆成两份。
        修复后：无论从哪里启动，都解析到同一绝对路径。
        """
        for field_name in ("chroma_persist_dir", "upload_dir"):
            value = getattr(self, field_name)
            if value and not Path(value).is_absolute():
                setattr(self, field_name, str((REPO_ROOT / value).resolve()))
        return self


settings = Settings()
