"""OpenTelemetry SDK setup shared by every cloud. Each cloud only supplies its span exporter."""

from collections.abc import Callable

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

SERVICE_NAME = "chatbot-api"
FLUSH_TIMEOUT_MS = 2000


def configure_tracing(exporter: SpanExporter, sample_ratio: float) -> Callable[[], None]:
    """Install the global tracer provider and return a flush function for the end of a request.

    Sampling honours a browser that asked for a trace (sampled parent), but does not trust a
    "not sampled" parent: Cloud Run's front end adds one to most requests, and honouring it would
    drop nearly every trace. Those requests, and requests with no parent, use `sample_ratio`.
    """
    ratio = TraceIdRatioBased(sample_ratio)
    provider = TracerProvider(
        resource=Resource.create({"service.name": SERVICE_NAME}),
        sampler=ParentBased(root=ratio, remote_parent_not_sampled=ratio),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Serverless runtimes throttle CPU once a response ends, so spans left in the batch queue
    # might not be sent until the next request. Flushing before the request ends sends them.
    def flush() -> None:
        provider.force_flush(timeout_millis=FLUSH_TIMEOUT_MS)

    return flush
