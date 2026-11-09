"""Structured logging.

Every log line, from Iki's own code, Django or uvicorn, goes through one
structlog formatter. Production output is one JSON object per line. Each
line carries the active OpenTelemetry trace and span IDs, so logs can be
joined with traces.
"""

import logging
import logging.config
from typing import IO, Any

import structlog
from opentelemetry import trace
from structlog.typing import EventDict, Processor, WrappedLogger


def add_trace_context(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Adds ``trace_id`` and ``span_id`` from the current OpenTelemetry span, if any."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def drop_color_message(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Removes uvicorn's ANSI-colored duplicate of the message."""
    event_dict.pop("color_message", None)
    return event_dict


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_trace_context,
        structlog.stdlib.ExtraAdder(),
        drop_color_message,
    ]


def build_logging_config(level: str, fmt: str, stream: IO[str] | None = None) -> dict[str, Any]:
    """Returns a ``logging.config.dictConfig`` dict that routes everything through structlog.

    Output goes to stdout unless ``stream`` is given (tests capture logs that way).
    """
    if fmt not in ("json", "console"):
        msg = f"IKI_LOG_FORMAT must be 'json' or 'console', not {fmt!r}"
        raise ValueError(msg)
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "structlog": {
                "()": structlog.stdlib.ProcessorFormatter,
                "foreign_pre_chain": _shared_processors(),
                "processors": [
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info,
                    renderer,
                ],
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": stream if stream is not None else "ext://sys.stdout",
                "formatter": "structlog",
            },
        },
        "root": {"handlers": ["stdout"], "level": level.upper()},
        "loggers": {
            # Uvicorn installs its own handlers; route its loggers to ours instead.
            # The access log is off: RequestLogMiddleware logs requests once,
            # without query strings, which may carry secrets.
            "uvicorn": {"handlers": [], "propagate": True},
            "uvicorn.error": {"handlers": [], "propagate": True},
            "uvicorn.access": {"handlers": [], "propagate": False, "level": "WARNING"},
            # Django logs 4xx/5xx through django.request; RequestLogMiddleware
            # already covers those, so keep only server errors with tracebacks.
            "django.request": {"level": "ERROR"},
            "django.server": {"level": "WARNING"},
        },
    }


def configure_logging(
    level: str = "INFO", fmt: str = "json", stream: IO[str] | None = None
) -> None:
    """Configures structlog and the standard library's logging in one step."""
    logging.config.dictConfig(build_logging_config(level, fmt, stream))
    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
