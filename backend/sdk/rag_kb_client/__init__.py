"""rag-kb-client——企业知识库 Python SDK（批次E·6c）

薄封装两个兼容门（零学习成本接入）：
- OpenAI 兼容对话门：POST /v1/chat/completions（与 OpenAI SDK 同协议）
- Dify 兼容检索门：POST /v1/dify/retrieval（Dify 工作流知识检索节点）

用法：
    from rag_kb_client import RagKbClient

    client = RagKbClient(base_url="http://localhost:8000",
                         api_key="sk-rag-xxx")
    # 对话（OpenAI 协议）
    answer = client.chat("年假怎么申请？")
    # 检索（Dify 协议）
    records = client.retrieve("报销流程是什么", top_k=3)
"""
import httpx


class RagKbClient:
    """企业知识库客户端：Bearer API key 鉴权（管理端 API Key 页生成）"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout

    # ── OpenAI 兼容对话门 ──

    def chat(self, question: str, history: list[dict] | None = None,
             stream: bool = False) -> str | httpx.Response:
        """单轮/多轮问答。history=[{"role":"user","content":"..."}] 多轮
        历史（不含当前问题）；stream=True 返回 httpx.Response 流式响应，
        调用方自行迭代 SSE。"""
        messages = list(history or []) + [{"role": "user", "content": question}]
        resp = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            headers=self._headers,
            json={"model": "rag-knowledge-base", "messages": messages,
                  "stream": stream},
            timeout=self._timeout,
        )
        if stream:
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    # ── Dify 兼容检索门 ──

    def retrieve(self, query: str, top_k: int = 3,
                 score_threshold: float = 0.0,
                 knowledge_id: str | None = None) -> list[dict]:
        """知识检索，返回 [{"content","score","title","metadata"}]。
        knowledge_id 可选 "kb:<库id>" 限定库（须在 key 可见范围内）。"""
        resp = httpx.post(
            f"{self.base_url}/v1/dify/retrieval",
            headers=self._headers,
            json={
                "query": query,
                "knowledge_id": knowledge_id,
                "retrieval_setting": {"top_k": top_k,
                                      "score_threshold": score_threshold},
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()["records"]
