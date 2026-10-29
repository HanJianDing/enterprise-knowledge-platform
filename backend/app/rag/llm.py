"""LLM 工厂 - 阿里云百炼 DashScope (Qwen) + OpenAI 兼容端点（私有化预留）

企业知识库改造 阶段6：配置 LLM_API_BASE（OpenAI 兼容端点，如内网 vLLM）
时生成改用该端点；不配置则走百炼云 API。迁内网私有化只需改 .env：
    LLM_API_BASE=http://<内网vLLM地址>/v1
    LLM_API_KEY=xxx（可选）
    LLM_MODEL=<本地模型名>
"""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
from langchain_community.chat_models import ChatTongyi
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from loguru import logger
from app.config import settings


def get_llm(streaming: bool = True, model: str | None = None,
             temperature: float | None = None):
    """获取 LLM 实例。

    - LLM_API_BASE 未配置：百炼 ChatTongyi（通义千问）
    - LLM_API_BASE 已配置：OpenAI 兼容端点（私有化 vLLM/自建服务）
    model: 指定模型名；None 时用 settings.llm_model。
    temperature: None 时用 settings.llm_temperature；批次4 起可显式传 0
      （实体消歧/社区匹配等确定性判断类调用必须 temperature=0——
      采样随机性会让同一问句两次答案不同）"""

    if settings.llm_api_base:
        return OpenAICompatibleChat(
            api_base=settings.llm_api_base.rstrip("/"),
            api_key=settings.llm_api_key,
            model=model or settings.llm_model,
            temperature=settings.llm_temperature if temperature is None else temperature,
        )
    return ChatTongyi(
        model=model or settings.llm_model,
        temperature=settings.llm_temperature if temperature is None else temperature,
        dashscope_api_key=settings.dashscope_api_key,
        streaming=streaming,
    )


class OpenAICompatibleChat(BaseChatModel):
    """OpenAI 兼容端点的聊天模型（依赖零新增，httpx 直连）。

    覆盖 _generate（invoke/分类）与 _stream（LCEL astream 流式生成）。
    流式解析 SSE（OpenAI 格式：data: {...} / data: [DONE]）。
    """

    api_base: str
    api_key: str = ""
    model: str = "default"
    temperature: float = 0.1

    @property
    def _llm_type(self) -> str:
        return "openai-compatible"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, messages: list, stream: bool) -> dict:
        return {
            "model": self.model,
            "messages": [
                {"role": m.type if m.type != "human" else "user", "content": m.content}
                for m in messages
            ],
            "temperature": self.temperature,
            "stream": stream,
        }

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        resp = httpx.post(
            f"{self.api_base}/chat/completions",
            json=self._payload(messages, stream=False),
            headers=self._headers(),
            timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"私有化 LLM 端点返回 {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage")
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=content))],
            llm_output={"token_usage": usage} if usage else None,
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        with httpx.stream(
            "POST",
            f"{self.api_base}/chat/completions",
            json=self._payload(messages, stream=True),
            headers=self._headers(),
            timeout=300,
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"私有化 LLM 端点返回 {resp.status_code}")
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: "):].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield ChatGenerationChunk(message=AIMessageChunk(content=delta))


# LLM 调用专用线程池（批次F 验收修复）：与共享 to_thread 池隔离——
# 上游挂起时线程最多只占满本池，不再拖垮检索/嵌入等所有共享池调用
# （真实验收事故：挂起线程累积后检索阶段也排队卡死）。max_workers=8：
# 非流式 LLM 调用（invoke_with_timeout 6 处——质检 P3-3 复核：全仓
# grep 实测；+ 问题拆解 decompose）并发量有限，8 个足够且限制残留面。
# 批次F 挂清单收敛：extract_calculation/图谱消歧/检索建议/工具决策等
# 其余同步 LLM 调用已全部迁入本池（run_in_llm_executor），全仓已无
# 同步 LLM 调用挂共享池（迁移时全仓 grep 复核）。
_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")


def run_in_llm_executor(func, *args):
    """在 LLM 专用池执行同步函数（asyncio 调用接口，返回 Future）——
    质检 P3-3：供 invoke_with_timeout 之外的同步 LLM 调用（问题拆解
    decompose、数值提取 extract_calculation、图谱消歧、检索建议、工具
    决策 detect_tool_call）使用，与共享池隔离；挂起线程最多占满专用池
    8 个。调用方自行决定超时包装（invoke_with_timeout 已含 wait_for）。"""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(_LLM_EXECUTOR, func, *args)


async def invoke_with_timeout(llm: BaseChatModel, prompt: str, timeout: float | None = None,
                              callbacks: list | None = None):
    """批次5 A1：非流式 LLM 调用带超时——超时返回 None（调用方静默降级），
    防上游接口挂起（网络黑洞）拖垮请求。底层 invoke 在专用线程池执行，
    超时后线程残留只占专用池（进程级无害），请求侧立即放弃等待。
    批次C.5 P3-12：callbacks 可选透传（反思自检等非流式调用挂
    TokenUsageHandler，token 用量并入统计口径）。"""
    timeout = timeout or settings.llm_timeout_seconds
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                _LLM_EXECUTOR,
                llm.invoke,
                prompt,
                {"callbacks": callbacks} if callbacks else None,
            ),
            timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "LLM 调用超时（>{}s），已放弃等待走降级: {}...", timeout, prompt[:50]
        )
        return None


class TokenUsageHandler(BaseCallbackHandler):
    """捕获生成阶段 LLM token 用量（归一化为 {prompt_tokens, completion_tokens, total_tokens}）"""

    def __init__(self):
        self.usage: dict | None = None

    def on_llm_end(self, response, **kwargs) -> None:
        """LLM 生成结束时捕获 token 用量"""
        try:
            llm_output = getattr(response, "llm_output", None) or {}
            usage = llm_output.get("token_usage") or llm_output.get("usage")
            if not usage:
                meta = getattr(response, "response_metadata", None) or {}
                usage = meta.get("token_usage")
            if usage:
                self.usage = {
                    "prompt_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                }
        except Exception as e:
            logger.warning(f"捕获 token 用量失败: {e}")
