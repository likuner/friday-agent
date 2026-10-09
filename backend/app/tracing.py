"""OpenTelemetry 追踪初始化。

把 Agent 的运行信息（模型调用、token 用量、耗时等）以标准 OTLP 协议导出，
可直接被 **AgentScope Studio** 可视化，也可接 Jaeger / Langfuse /
Arize-Phoenix 等任意 OTLP 后端。

关于接入方式的说明
------------------
LangGraph 迁移前由 ``agentscope.middleware.TracingMiddleware`` 产出符合
OpenTelemetry GenAI 语义约定的 span；迁移后由自研的
``app/tracing_callback.py``（LangChain BaseCallbackHandler）产出**基础版**
span（模型调用级，属性带模型名/耗时/token），挂在 ChatDeepSeek 的
callbacks 上。

Studio 侧对外暴露的正是标准 OTLP 端点（见其官方开发文档）：

- OTLP/gRPC  ``localhost:4317``   —— 可用 ``OTEL_GRPC_PORT`` 调整
- OTLP/HTTP  ``localhost:3000``   —— 即 Studio 的 Web 端口，可用 ``PORT`` 调整

因此无需任何 Studio 专用 SDK，按下面的配置指向对应端点即可。
"""

import logging

from .config import settings

logger = logging.getLogger("friday.tracing")

_configured = False


def setup_tracing() -> bool:
    """按配置初始化全局 TracerProvider。

    未启用或依赖缺失时返回 ``False``；此时 tracing_callback 不挂任何回调，
    对调用链几乎没有开销。
    """
    global _configured
    if _configured:
        return True
    if not settings.tracing_enabled:
        logger.info("节点[追踪] 未启用 tracing_enabled=false")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcSpanExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("节点[追踪] 缺少 opentelemetry 依赖，已跳过启用")
        return False

    protocol = (settings.otel_exporter_otlp_protocol or "grpc").lower()
    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    exporter_kwargs: dict[str, str] = {}
    if endpoint:
        if protocol == "http":
            # 注意：显式传 endpoint 时 SDK 不会自动补信号路径（只有读
            # OTEL_EXPORTER_OTLP_ENDPOINT 环境变量时才会补），这里手动补齐，
            # 让该项保持"基址"语义，如 http://localhost:3000 -> /v1/traces
            endpoint = endpoint.rstrip("/")
            if not endpoint.endswith("/v1/traces"):
                endpoint = f"{endpoint}/v1/traces"
        exporter_kwargs["endpoint"] = endpoint
    # 不传 endpoint 时，SDK 会回退读 OTEL_EXPORTER_OTLP_* 标准环境变量
    exporter = HttpSpanExporter(**exporter_kwargs) if protocol == "http" else GrpcSpanExporter(**exporter_kwargs)

    provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _configured = True
    logger.info(
        "节点[追踪] 已启用 protocol=%s endpoint=%s service=%s",
        protocol,
        exporter_kwargs.get("endpoint", "(读取 OTEL_* 环境变量)"),
        settings.otel_service_name,
    )
    return True


def shutdown_tracing() -> None:
    """进程退出前刷新尚未发送的 span，避免丢失尾部数据。"""
    if not _configured:
        return
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
            logger.info("节点[追踪] 已刷新并关闭")
    except Exception:
        logger.exception("节点[追踪] 关闭异常")
