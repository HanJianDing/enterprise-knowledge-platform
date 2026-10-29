"""图片问答（批次D·11d）——picture 模板（借鉴 RAGFlow）

图片文档（PNG/JPG）的处理：OCR 抠字 + 多模态 LLM 看图说话，
两条路合成"图片文档文本"入库——图片知识可检索、可问答。
- OCR 文本：图里的文字（流程图步骤、表格字样）
- vision 描述：图的内容语义（"这是报销流程：提交申请→部门审批→打款"）
合成文本以 structured 单块入库（合成文本一体，检索零特殊处理）。

隐私说明（批次D 质检 P3 + 批次D 后续更新）：dashscope provider 下图片
经 file:// 引用上传其临时存储（内容出本地）；数据不出域场景配
VISION_API_BASE 切 openai_compat provider（本地多模态端点，图片
base64 直传不落地外部存储）——provider 已预留，部署触发制。
"""
from pathlib import Path

from loguru import logger

from app.config import settings

VISION_PROMPT = """你是企业知识库的图片文档整理员。请描述这张图片的内容，要求：
1. 先判断图片类型（流程图/表格/组织结构图/照片/截图等）
2. 用结构化文字描述图中的完整信息：图中的文字内容、箭头与流程关系、
   层级结构、关键数据
3. 描述要能让"只看文字的人"完整理解图片内容（图片本身不会被检索，
   这段描述就是它唯一的可检索载体）
4. 150~400 字，直接输出描述正文，不要任何前缀"""


def describe_image(file_path: str) -> str:
    """多模态 LLM 看图说话；失败/未配置返回空串（静默降级——OCR 文本
    仍在，图片仍有部分可检索性）。同步阻塞调用——调用方在文档处理的
    to_thread 线程池内执行。

    批次D 后续：provider 抽象（vision_provider 配置）——
    dashscope=百炼云（现状）；openai_compat=本地多模态端点（数据不出域
    场景配 VISION_API_BASE，图片 base64 直传本地、不再上传外部存储）。
    """
    if settings.vision_provider == "openai_compat" and settings.vision_api_base:
        return _describe_openai_compat(file_path)
    return _describe_dashscope(file_path)


def _describe_dashscope(file_path: str) -> str:
    """百炼云 qwen3-vl（默认 provider）"""
    try:
        import dashscope
        from dashscope import MultiModalConversation

        messages = [{
            "role": "user",
            "content": [
                {"image": f"file://{file_path}"},
                {"text": VISION_PROMPT},
            ],
        }]
        # qwen3-vl-plus（2026-10 实测可用；qwen-vl-plus/qwen-vl-max 已下线
        # 404、qwen2.5-vl 403——模型下线迁移与分类模型 qwen-flash 同批公告）
        # 批次D 质检 P2-1：显式 request_timeout=120s（SDK 默认 300s 总超时，
        # 挂起期间占用线程池 worker——并发图片上传会耗尽共享线程池）
        response = MultiModalConversation.call(
            model=settings.vision_model,
            api_key=settings.dashscope_api_key,
            messages=messages,
            request_timeout=120,
        )
        if response.status_code != 200:
            logger.warning("vision 描述失败（HTTP {}）: {}",
                           response.status_code,
                           (response.message or "")[:200])
            return ""
        content = response.output.choices[0].message.content
        # 多模态返回是 list[{"text": ...}] 结构
        texts = [c.get("text", "") for c in content if isinstance(c, dict)]
        return "\n".join(texts).strip()
    except Exception as e:
        logger.warning("vision 描述异常（静默降级）: {}", e)
        return ""


def _describe_openai_compat(file_path: str) -> str:
    """OpenAI 兼容多模态端点（本地 vLLM 等）——图片 base64 直传，
    数据不出域；密钥回退链与 rerank 私有化预留同款"""
    import base64

    import httpx

    try:
        img_b64 = base64.b64encode(
            Path(file_path).read_bytes()
        ).decode("ascii")
        # 扩展名 → MIME（本地端点常见的两种输入格式兼容）
        ext = Path(file_path).suffix.lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        payload = {
            "model": settings.vision_model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime};base64,{img_b64}"}},
                ],
            }],
            "max_tokens": 800,
        }
        headers = {"Content-Type": "application/json"}
        # 批次D后续质检低危#8：未配 vision_api_key 时回退复用通用/云端 key——
        # 与 reranker 既有设计一致（端点内网可信），但复用云端 key 会把它
        # 发往本地端点，留 warning 提示运维确认（仅在确有可回退 key 时告警，
        # 三个 key 全空时无回退源、请求不带 Authorization，"回退复用"不成立）
        if not settings.vision_api_key and (
                settings.llm_api_key or settings.dashscope_api_key):
            logger.warning(
                "vision(openai_compat) 未配 vision_api_key，回退复用"
                " llm/dashscope key（若为云端 key 将发送至本地端点）"
            )
        key = settings.vision_api_key or settings.llm_api_key \
            or settings.dashscope_api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        resp = httpx.post(
            f"{settings.vision_api_base.rstrip('/')}/chat/completions",
            headers=headers, json=payload, timeout=120,
        )
        if resp.status_code != 200:
            logger.warning("vision(openai_compat) 失败（HTTP {}）: {}",
                           resp.status_code, resp.text[:200])
            return ""
        content = resp.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):  # 部分端点返回多段结构
            content = "\n".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
        return (content or "").strip()
    except Exception as e:
        logger.warning("vision(openai_compat) 异常（静默降级）: {}", e)
        return ""


def build_picture_text(filename: str, ocr_text: str, vision_desc: str,
                       table_markdown: str = "") -> str:
    """合成图片文档入库文本：vision 描述 + 表格结构 + OCR 文字
    （picture 模板核心；批次D 后续加【表格内容】段——识别到表格时
    Markdown 结构文本入库，行列结构可检索）"""
    parts = [f"【图片文档】{filename}"]
    if vision_desc:
        parts.append(f"【图片内容描述】\n{vision_desc}")
    if table_markdown:
        parts.append(f"【表格内容】\n{table_markdown}")
    if ocr_text:
        parts.append(f"【图中文字】\n{ocr_text}")
    return "\n\n".join(parts)
