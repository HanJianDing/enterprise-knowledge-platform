"""重排 provider 抽象（验收批次1·1e）——检索候选精排

P3-3：混合检索粗召回（RECALL_K 条候选）后，用重排模型把
「问题 vs 每条文档」两两对照打分，按相关性取前 top_n 条喂给 LLM。
失败时降级为原顺序返回，不阻断问答主流程。

验收批次1·1e：模型可插拔——原实现把 gte-rerank-v2 写死单实现；
现拆成 provider 抽象（BaseReranker 协议），两种实现：
- DashScopeReranker：百炼云（现有逻辑，默认）
- OpenAICompatReranker：OpenAI 兼容端点（内网 vLLM/自建重排服务，
  请求 Cohere 风格 /v1/rerank，响应兼容 results[index+relevance_score]）
由 settings.rerank_provider 选择，换模型/换服务只改配置不改代码。
"""
import httpx
from loguru import logger

from app.config import settings


class BaseReranker:
    """重排 provider 协议：返回按相关性降序的文档列表（异常时降级原顺序）。

    boosts（批次语料增强）：{document_id: 系数}——排序分乘以系数（通知时效
    衰减等时态信号在 rerank 层生效；纯语义排序会把内容几乎相同的跨年度
    通知排乱，衰减必须在精排层介入）。
    """

    def rerank(self, query: str, docs: list, top_n: int = 6,
               boosts: dict[str, float] | None = None) -> list:
        raise NotImplementedError


class DashScopeReranker(BaseReranker):
    """百炼云重排（gte-rerank-v2，默认）"""

    URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"

    def rerank(self, query: str, docs: list, top_n: int = 6,
               boosts: dict[str, float] | None = None) -> list:
        if not settings.dashscope_api_key:
            logger.warning("未配置 DASHSCOPE_API_KEY，rerank 降级为原顺序")
            return docs[:top_n]
        try:
            resp = httpx.post(
                self.URL,
                headers={
                    "Authorization": f"Bearer {settings.dashscope_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.rerank_model,
                    "input": {
                        "query": query,
                        "documents": [d.page_content for d in docs],
                    },
                    "parameters": {"top_n": min(top_n, len(docs))},
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            results = resp.json()["output"]["results"]
            # 批次语料增强：relevance_score × 时效系数后重排（不再直接信任
            # API 顺序——衰减信号需要介入精排层）。
            # 批次语料增强二期：加权后的分数写进 doc.metadata["rerank_score"]
            # ——低相关判定（web 搜索兜底触发条件）需要"排序用的同一分数"
            boosted = [
                (r.get("relevance_score", 0)
                 * (boosts or {}).get(
                     docs[r["index"]].metadata.get("document_id", ""), 1.0),
                 r)
                for r in results
            ]
            boosted.sort(key=lambda p: p[0], reverse=True)
            ranked = []
            for score, r in boosted[:top_n]:
                doc = docs[r["index"]]
                doc.metadata["rerank_score"] = round(score, 6)
                ranked.append(doc)
            logger.info(f"rerank(dashscope) 完成: {len(docs)} 条候选 → 取 {len(ranked)} 条")
            return ranked
        except Exception as e:
            logger.warning(f"rerank(dashscope) 失败，降级为原顺序: {e}")
            return docs[:top_n]


class OpenAICompatReranker(BaseReranker):
    """OpenAI 兼容重排端点（内网自建/vLLM 等）

    请求：POST {base}/rerank {"model", "query", "documents", "top_n"}
    响应兼容两种常见格式（Cohere 风格与 dashscope 兼容风格），
    均按 relevance_score 降序取 top_n。
    """

    def rerank(self, query: str, docs: list, top_n: int = 6,
               boosts: dict[str, float] | None = None) -> list:
        base = settings.rerank_api_base.rstrip("/")
        # 密钥回退为有意设计：内网端点未配独立 key 时复用现有云端 key
        # （内网服务多为免鉴权/共享密钥，少一套密钥配置负担）
        key = settings.llm_api_key or settings.dashscope_api_key
        if not base:
            logger.warning("未配置 RERANK_API_BASE，OpenAI 兼容重排降级为原顺序")
            return docs[:top_n]
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            resp = httpx.post(
                f"{base}/rerank",
                headers=headers,
                json={
                    "model": settings.rerank_model,
                    "query": query,
                    "documents": [d.page_content for d in docs],
                    "top_n": min(top_n, len(docs)),
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            # 两种响应结构归一：results: [{index, relevance_score}]
            results = data.get("results") or (data.get("output") or {}).get("results") or []
            # 批次语料增强：relevance_score × 时效系数后重排；批次语料增强
            # 二期：加权分数写进 metadata["rerank_score"]（低相关判定用，
            # 与 dashscope provider 同款口径）
            boosted = [
                (x.get("relevance_score", 0)
                 * (boosts or {}).get(
                     docs[x["index"]].metadata.get("document_id", ""), 1.0),
                 x)
                for x in results
            ]
            boosted.sort(key=lambda p: p[0], reverse=True)
            ranked = []
            for score, x in boosted[:top_n]:
                doc = docs[x["index"]]
                doc.metadata["rerank_score"] = round(score, 6)
                ranked.append(doc)
            logger.info(f"rerank(openai_compat) 完成: {len(docs)} 条候选 → 取 {len(ranked)} 条")
            return ranked
        except Exception as e:
            logger.warning(f"rerank(openai_compat) 失败，降级为原顺序: {e}")
            return docs[:top_n]


def _get_reranker() -> BaseReranker:
    """按 settings.rerank_provider 选择实现（未知值回落 dashscope 保底）"""
    if settings.rerank_provider == "openai_compat":
        return OpenAICompatReranker()
    return DashScopeReranker()


def rerank_documents(query: str, docs: list, top_n: int = 6,
                     boosts: dict[str, float] | None = None) -> list:
    """对候选文档精排，返回按相关性降序的 top_n 条。

    Args:
        query: 用户问题（澄清后）
        docs: 候选文档列表（langchain Document，需有 page_content）
        top_n: 返回条数
        boosts: 批次语料增强——{document_id: 系数}，排序分乘系数
            （通知时效衰减在精排层介入；None=纯语义排序）

    Returns:
        排序后的文档列表。任何异常都降级为原顺序前 top_n 条。
    """
    if not docs:
        return docs
    if len(docs) == 1:
        return docs
    return _get_reranker().rerank(query, docs, top_n, boosts)
