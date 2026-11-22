"""Local-only tracing with the public OpenTelemetry Python SDK.

Spans go to a JSONL file through a small custom exporter. There is no network exporter.
The tracer provider is private to this module (not installed as the global provider).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Sequence

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult


class JsonlSpanExporter(SpanExporter):
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            for s in spans:
                ctx = s.get_span_context()
                f.write(
                    json.dumps(
                        {
                            "trace_id": format(ctx.trace_id, "032x"),
                            "span_id": format(ctx.span_id, "016x"),
                            "parent_span_id": format(s.parent.span_id, "016x") if s.parent else None,
                            "name": s.name,
                            "start_ns": s.start_time,
                            "end_ns": s.end_time,
                            "status": s.status.status_code.name,
                            "attributes": {k: v for k, v in (s.attributes or {}).items()},
                        }
                    )
                    + "\n"
                )
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def make_tracer(path: Path | str):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(path)))
    return provider.get_tracer("chinook_mcp")
