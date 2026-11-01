"""GCP observability: spans to Cloud Trace, and JSON logs that Cloud Logging links to the trace."""

import json
import logging
import sys

from opentelemetry import trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.sdk.trace.export import SpanExporter


def cloud_trace_exporter(project_id: str) -> SpanExporter:
    # The exporter's constructor has no type hints upstream.
    return CloudTraceSpanExporter(project_id=project_id)  # type: ignore[no-untyped-call]


class CloudLoggingFormatter(logging.Formatter):
    """One JSON object per line, the structured format Cloud Run sends to Cloud Logging.

    The trace fields make each log line appear under its request's trace. Exceptions are logged
    by type only, never by message, because messages can quote user text.
    """

    def __init__(self, project_id: str) -> None:
        super().__init__()
        self._project_id = project_id

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["error.type"] = record.exc_info[0].__name__
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            entry["logging.googleapis.com/trace"] = (
                f"projects/{self._project_id}/traces/{context.trace_id:032x}"
            )
            entry["logging.googleapis.com/spanId"] = f"{context.span_id:016x}"
            entry["logging.googleapis.com/trace_sampled"] = context.trace_flags.sampled
        return json.dumps(entry, ensure_ascii=False)


def configure_cloud_logging(project_id: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudLoggingFormatter(project_id))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
