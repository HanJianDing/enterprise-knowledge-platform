# RAGFlow 差距分析与迭代路线图（自研项目升级参照）

> 参照对象：RAGFlow 0.27.2（D:\ragflow-main，Python 3.13 + Go 双引擎）
> 分析对象：自研企业知识库 RAG 系统（FastAPI + PostgreSQL + ChromaDB）
> 产出日期：2026-10-01
> 定位：RAGFlow 需要 16GB 启动，对应的是它完整的能力版图——它的功能与能力确实强于自研项目。
> 本报告只回答两个问题：①我的项目比它少什么；②哪些值得借鉴、按什么顺序迭代。

## 一、先拆解：16GB 到底花在哪，哪些能力是"重"的、哪些是"白捡"的

| RAGFlow 重量级组件 | 内存/成本 | 换来的能力 | 自研项目要不要 |
|---|---|---|---|
| ES/OpenSearch/Infinity 混合索引 | 2~4GB | 亿级索引、全文+向量落盘、横向扩容 | 500 人规模不需要引擎本身，但它的**检索信号设计**（同义词/词权重/pagerank）零成本可捡 |
| deepdoc OCR 服务（ONNX 模型） | 1~2GB | 扫描件/图片 PDF 的版面分析、表格识别 | 企业文档常见形态，**值得按需引入** |
| TEI 本地嵌入模型 | 1~2GB | 不依赖云 API 的嵌入推理 | 我们走 dashscope 云 API，不需要 |
| MySQL + Valkey + MinIO + NATS + Kvrocks | ~2GB | 多租户元数据 + 任务队列 + 对象存储 | 单租户 PG + job 表已覆盖；队列思路可借鉴 |
| Go 双引擎重写 | 数百 MB | 高并发 ingest/API | 单机规模不需要 |
| Agent 代码沙箱容器 | 1GB+ | Agent 执行任意代码 | 与"受限 Agent"哲学冲突，不做 |
| ClickHouse/Jaeger 等可观测 | 数百 MB | APM/链路追踪 | 4C4G 时期"够用版"（健康检查+统计仪表+手册）即可；**16G 升级后按 P0 引入真实栈**（用户 2026-10-01 拍板） |

**结论：RAGFlow 的"重"有一半花在规模化能力上（多租户/亿级索引/双引擎/沙箱），我们确实用不上；但它的"软件级能力"——提示词体系、API 兼容层、检索信号、Agent 编排、图谱管线——绝大多数是轻的。迭代策略 = 白捡轻能力，选择性上重能力。**

## 二、差距全景图（自研没有的架构与模块，按能力域分组）

### 域 1：文档解析（差距最大）
- deepdoc：15+ 格式 parser；**OCR、版面分析、表格结构识别**（独立 ONNX 服务，CPU 可跑）
- 图片问答：picture 模板 + img2txt + vision prompt（产品图/流程图问答）
- 自研现状：pypdf/python-docx/openpyxl/markdown，无 OCR、无版面分析、无图片理解

### 域 2：索引与检索
- **全文+向量双引擎**（ES/Infinity）+ **三信号加权**（token 全文 + vector 向量 + rank_feature pagerank）
- **同义词表 + 词权重**（企业术语提准）、**TOC 感知检索**、text2sql、web search 兜底
- **检索参数按 KB 全暴露**（阈值/权重/top_k/候选数/knn 参数）
- 自研现状：MMR+BM25 RRF + 单一 rerank；参数在代码内，无同义词/词权重/pagerank 信号

### 域 3：分块
- 15 类分块模板（naive/laws/qa/table/book/paper/manual/presentation/resume/picture/one/audio/email/kg/tag）
- **TitleChunker 标题层级分块** + **TOC 感知检索**（长文档章节级召回）
- RAPTOR 递归摘要树、子分块（sub-chunk）
- 自研现状：3 模板（条款/问答对/表格）+ 标题层级（已有雏形）+ 递归兜底；无 TOC 检索、无 RAPTOR

### 域 4：知识图谱（已立项，见《知识图谱检索实施方案》）
- GraphRAG light/general（LLM 抽取）、NER 无 LLM 抽取、实体消歧（消解）、Leiden 社区报告、结构图编译（evidence gate）

### 域 5：Agent 引擎
- Canvas DSL：24 组件（begin/message/llm/switch/loop/iteration/categorize/fillup/invoke/browser/excel_processor/docs_generator/agent_with_tools…）
- 20 个 JSON 模板（deep_research/text2sql/customer_service…）、28 个外部工具、长期记忆 memory service、代码沙箱、浏览器 agent
- advanced_rag 编排器（子问题编排、sufficiency 判断、关键词、算术模块）
- 自研现状：固定三步 Agentic RAG（拆解≤4→多路检索→汇总）+ ast 白名单计算器，无反思循环、无工具生态

### 域 6：生态与接口
- **OpenAI 兼容对话 API**（/openai/<chat_id>/chat/completions）、**Dify 兼容检索 API**、Python SDK
- 数据源连接器（Notion/S3/Discord/RSS/sitemap/飞书 wiki 同步）
- 多通道接入（企微/钉钉/Discord）；TTS/ASR 语音
- 自研现状：仅自用 REST /api/v1

### 域 7：平台能力
- 多租户 SaaS（Tenant/UserTenant/APIToken）、OAuth/OIDC/GitHub 登录、i18n、插件机制
- 自研现状：单租户企业定位（部门-库授权矩阵反而更贴合企业内网）

### 域 8：工程化
- Redis 队列 + task_executor 多进程 worker + **并发限流**；**文件版本管理**（file_commit，制度更新回溯）；Jaeger 链路追踪；pipeline 阶段录制
- 自研现状：job 表状态机 + 解析事件流水时间轴（产品化更好）；无版本管理、无并发限流

## 三、迭代路线图（P0 / P1 / P2，含工作量与理由）

### P0：立即可做（轻而值钱，1~2 周内可全部落地）

| # | 模块 | 工作量 | 对自研项目的意义 |
|---|---|---|---|
| 1 | **GraphRAG 图谱检索**（已立项，见实施方案） | 5~8 人日 | 补上多跳关系类问题，检索第三腿 |
| 2 | **可观测栈：OTEL + Jaeger + ClickHouse**（16G 升级配套，已拍板） | 4~5 人日 | 埋点 RAG 管线各阶段；locust 压测→Jaeger 定位瓶颈→优化前后 P95 对比的演示故事；行业通用技能 |
| 3 | **OpenAI 兼容对话 API** | 1~2 人日 | 十几行代码接入任意 LLM 客户端/工作流平台，生态分直接拉满 |
| 4 | **检索提准三件套**：同义词表 + 词权重 + pagerank 信号 | 2~3 人日 | BM25 腿的提准利器；企业术语（如"报销=费用报销"）同义词尤其见效；pagerank 用文档引用关系即可算，零新依赖 |
| 5 | **KB 级检索参数配置**（阈值/top_k/权重按库可调） | 1~2 人日 | 检索调优从改代码变改配置，演示时现场调参是加分项 |
| 6 | **TitleChunker 标题层级分块 + TOC 感知检索** | 2~3 人日 | 长手册/制度类文档的章节级召回；我们已有标题路径前缀雏形，补齐目录索引即可 |

### P1：中期（1 个月内，按需上）

| # | 模块 | 工作量 | 理由与取舍 |
|---|---|---|---|
| 6 | **deepdoc OCR 引入**（独立容器或 ONNX 服务） | 1 周 | 扫描件 PDF 是企业文档刚需；与自研解析器做"规整文档走轻解析、扫描件走 OCR"的分流 |
| 7 | **MCP client 角色** | 3~5 人日 | 与自研 MCP server 呼应：客服 Agent 可调外部 MCP 工具，进可攻退可守 |
| 8 | **文件版本管理** | 1 周 | 制度修订频繁时版本回溯；审计场景增值 |
| 9 | **图片问答** | 2~3 人日 | 依赖多模态模型预算；产品图/流程图问答演示效果好 |
| 10 | **web search 兜底 / text2sql** | 各 2~3 人日 | web 兜底公网场景可用；text2sql 企业内网价值一般，降级做 |

### P2：远期按需（不轻易做）

| # | 模块 | 理由 |
|---|---|---|
| 11 | Agent Canvas 组件化编排 | 与自研"受限 Agentic RAG"（延迟可预期、防失控）哲学冲突；除非要转型低代码平台，否则不做 |
| 12 | 多租户 SaaS | 单租户企业定位，部门-库授权矩阵已更贴合；多租户是另一个产品方向 |
| 13 | 连接器/多通道（企微/钉钉） | 企微通道按原定阶段 D 计划；其余等实际需求 |
| 14 | RAPTOR / 长期记忆 / TTS / 代码沙箱 / 浏览器 agent | 超出当前定位；RAPTOR 在长文档问答有真实效果时可再评估 |

## 四、自研保留的优势（迭代方向：补齐它的长处，保住我们的长处）

| # | 自研优势 | RAGFlow 对应情况 |
|---|---|---|
| 1 | 部门-库授权矩阵 + 个人覆盖 + fail-closed 检索作用域 | 仅租户级 me\|team 粗粒度 |
| 2 | JWT 双 token + session_version 顶号 + 密码策略 + SSO 插槽 | 无顶号机制 |
| 3 | 全链路业务审计（含 MCP 每次调用留痕）+ 审计导出 | 仅管道日志 + Langfuse 追踪 |
| 4 | MCP 权限穿透（JWT + mcpk_ 凭证双通道、sha256、可撤销） | 仅 api key 级鉴权 |
| 5 | 内置评估体系（评估集跑分可视化 + 压测基线） | 无内置评估模块 |
| 6 | 上传任务持久化 + 解析流水时间轴 | 有 Task 表但无用户级时间轴产品 |
| 7 | 受限 Agentic RAG（延迟可预期 + ast 白名单防注入） | advanced_rag 循环式编排，更重更不可控 |

## 五、一句话总结

RAGFlow 的能力版图 = 我们的差距清单；它的 16GB 里有一半是规模化能力（多租户/亿级索引/沙箱），我们不需要，但另一半是**轻的软件级能力**（API 兼容、检索信号、Agent 编排、图谱管线、提示词体系），应当按 P0→P1→P2 的顺序系统性地捡回来。迭代完成后，自研项目 = **RAGFlow 的核心检索能力 × 企业级权限审计 × 单机轻量部署**，三张牌各有胜负手。
