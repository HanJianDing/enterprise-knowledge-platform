"""可观测性（批次4）——OpenTelemetry 埋点：RAG 管线各阶段 span + trace 桥接

- init_tracing()：按 settings 选择导出器——otel_exporter=console（本地开发，
  trace 打到日志）或 otlp（上报 OTEL Collector → Jaeger + ClickHouse）；
  otel_enabled=False 时返回 NoopTracerProvider（零成本关闭）
- get_tracer()：惰性单例 tracer
- rag_span()：RAG 管线阶段的 span 上下文管理器（带阶段属性：
  分类/检索/重排/图谱/规则/生成，命中块数、top_k、耗时）
- get_current_trace_id()：回答链路 trace_id 桥接（前端展示 + Jaeger 跳转）

设计原则（与四检索腿一致的降级哲学）：可观测是增强能力，OTEL 包缺失、
Collector 不在线时全部静默降级，绝不影响问答主链路。
"""
import logging
import time
from contextlib import contextmanager

from app.config import settings

logger = logging.getLogger(__name__)

_tracer = None
_inited = False


def init_tracing(app=None) -> None:
    """应用启动时初始化 tracing（幂等；失败静默降级）。
    app 传入时挂 FastAPI 请求级插桩（质检 S1 修复：请求 span 作为根——
    无请求级 span 时 done 事件处当前 span 无效，trace_id 桥接恒为 null）。"""
    global _tracer, _inited
    if _inited:
        return
    _inited = True
    if not settings.otel_enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        from opentelemetry.sdk.resources import Resource

        # 批次5.5：resource 服务名（Jaeger UI 按服务名筛选，此前显示 unknown_service）
        resource = Resource.create({"service.name": settings.otel_service_name})
        if settings.otel_exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            exporter = OTLPSpanExporter(endpoint=settings.otel_endpoint)
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        else:  # console：本地开发验证埋点（无 Collector 也能看 trace 结构）
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

            provider = TracerProvider(resource=resource)
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("rag-knowledge-base")
        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            # excluded_urls 签名是 str（0.65b0 的 parse_excluded_urls 对字符串
            # 按逗号 split，传 "/health" 解析正确；质检 M2 核实无风险）
            FastAPIInstrumentor.instrument_app(app, excluded_urls="/health")
        logger.info(f"OTEL tracing 初始化完成（exporter={settings.otel_exporter}）")
    except Exception as e:
        # stdlib logging 用 %s 占位符（不能用 loguru 的 {}——格式化会失败）
        logger.warning("OTEL 初始化失败，静默降级为无追踪: %s", e)
        _tracer = None


def get_tracer():
    """惰性 tracer（未初始化返回 None，调用方跳过埋点）"""
    return _tracer


def reset_tracing() -> None:
    """批次5 A5：重置初始化状态——测试环境换 exporter 重新初始化用；
    生产无场景（_inited 只防重复 init 幂等）"""
    global _tracer, _inited
    _tracer = None
    _inited = False


@contextmanager
def rag_span(name: str, **attributes):
    """RAG 管线阶段 span：with rag_span("retrieval", top_k=6) as span:
    span.set_attribute("hit_chunks", n)。OTEL 不可用时为 no-op。"""
    tracer = get_tracer()
    if tracer is None:
        yield None
        return
    from opentelemetry import trace

    with tracer.start_as_current_span(name) as span:
        for k, v in attributes.items():
            span.set_attribute(k, v)
        yield span


def get_current_trace_id() -> str | None:
    """当前上下文 trace_id（回答 SSE 流中桥接给前端）。
    依赖请求级 span（FastAPI 插桩）——无请求级插桩时此处恒为 None
    （质检 S1 修复后：init_tracing(app) 挂了 FastAPIInstrumentor）"""
    try:
        from opentelemetry import trace

        current = trace.get_current_span().get_span_context()
        if current.is_valid:
            return trace.format_trace_id(current.trace_id)
    except Exception:
        pass
    return None
