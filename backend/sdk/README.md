# rag-kb-client

企业知识库（RAG）Python SDK——两个兼容门的薄封装，零学习成本接入。

## 安装

```bash
pip install ./sdk          # 本地路径安装（发布 PyPI 后 pip install rag-kb-client）
```

## 使用

```python
from rag_kb_client import RagKbClient

client = RagKbClient(
    base_url="http://localhost:8000",   # 部署地址
    api_key="sk-rag-xxx",              # 管理端 API Key 页生成
)

# 1. 对话（OpenAI 协议，与 OpenAI SDK 同构）
answer = client.chat("年假怎么申请？")
print(answer)

# 多轮历史
answer = client.chat("那最多能休几天？", history=[
    {"role": "user", "content": "年假怎么申请？"},
    {"role": "assistant", "content": "通过 OA 提交申请，部门负责人审批。"},
])

# 流式（SSE 响应，自行迭代）
resp = client.chat("报销流程是什么", stream=True)
for line in resp.iter_lines():
    print(line)

# 2. 检索（Dify 协议，可作 Dify 工作流知识节点同构调用）
records = client.retrieve("报销流程是什么", top_k=3)
for r in records:
    print(r["title"], r["score"], r["content"][:80])

# 限定知识库（"kb:<库id>"，须在 key 可见范围内）
records = client.retrieve("考勤规定", knowledge_id="kb:<库id>")
```

## 协议对应

| 方法 | 端点 | 协议 |
|---|---|---|
| `chat()` | POST /v1/chat/completions | OpenAI Chat Completions |
| `retrieve()` | POST /v1/dify/retrieval | Dify External Knowledge API |
