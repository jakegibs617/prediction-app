import logging
import logging.handlers
import sys
from contextvars import ContextVar

import structlog

from app.config import settings

# Thread-local correlation ID propagated through all log events in a pipeline run
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_SENSITIVE_KEYS = frozenset({
    "password", "api_key", "secret_key", "webhook_url",
    "anthropic_api_key", "openai_api_key", "groq_api_key",
    "telegram_bot_token", "telegram_chat_id", "admin_password",
})


def _mask_sensitive(logger, method, event_dict):
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "***"
    return event_dict


def _inject_correlation_id(logger, method, event_dict):
    cid = correlation_id_var.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging() -> None:
    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _inject_correlation_id,
        _mask_sensitive,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    if settings.log_to_stdout:
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.handlers.RotatingFileHandler(
            settings.log_file_path,
            maxBytes=52_428_800,
            backupCount=14,
            encoding="utf-8",
        )

    handler.setFormatter(formatter)
    root.handlers = [handler]
