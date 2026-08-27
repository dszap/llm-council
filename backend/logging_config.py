"""Safe structured logging primitives for the LLM Council backend."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 14
DEFAULT_TOTAL_MAX_BYTES = 500 * 1024 * 1024
VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
SENSITIVE_KEYS = re.compile(
    r"(?:authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|password|secret|token|api[_-]?key)",
    re.IGNORECASE,
)
TOKEN_VALUE = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-(?:or-)?[a-z0-9_-]*[a-z0-9_-]{8,}|eyJ[a-z0-9_-]{10,}\.[a-z0-9_-]+\.[a-z0-9_-]+)"
)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_request_id = contextvars.ContextVar("request_id", default=None)
_conversation_id = contextvars.ContextVar("conversation_id", default=None)


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    backend_level: str = "INFO"
    uvicorn_level: str = "INFO"
    vite_level: str = "INFO"
    browser_level: str = "WARNING"
    log_dir: Path = Path("logs")
    max_bytes: int = DEFAULT_MAX_BYTES
    retention_days: int = DEFAULT_RETENTION_DAYS
    total_max_bytes: int = DEFAULT_TOTAL_MAX_BYTES
    log_llm_payloads: bool = False
    browser_batch_size: int = 20
    browser_flush_ms: int = 2000
    browser_queue_limit: int = 200
    event_max_bytes: int = 65536

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        warning_sink: Callable[[str], None] = print,
    ) -> "LoggingSettings":
        values = os.environ if env is None else env
        return _settings_from_mapping(values, warning_sink)

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "backend_level": self.backend_level,
            "uvicorn_level": self.uvicorn_level,
            "vite_level": self.vite_level,
            "browser_level": self.browser_level,
            "log_dir": str(self.log_dir),
            "max_bytes": self.max_bytes,
            "retention_days": self.retention_days,
            "total_max_bytes": self.total_max_bytes,
            "log_llm_payloads": self.log_llm_payloads,
            "browser_batch_size": self.browser_batch_size,
            "browser_flush_ms": self.browser_flush_ms,
            "browser_queue_limit": self.browser_queue_limit,
            "event_max_bytes": self.event_max_bytes,
        }


@dataclass(frozen=True)
class LogContextTokens:
    request_id: contextvars.Token
    conversation_id: contextvars.Token


def bind_log_context(request_id: str | None, conversation_id: str | None) -> LogContextTokens:
    """Bind request identifiers to the current async/task context."""
    return LogContextTokens(_request_id.set(request_id), _conversation_id.set(conversation_id))


def reset_log_context(tokens: LogContextTokens) -> None:
    """Restore the context that was active before ``bind_log_context``."""
    _request_id.reset(tokens.request_id)
    _conversation_id.reset(tokens.conversation_id)


def truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool, int]:
    """Return a UTF-8-safe prefix and metadata about an optional truncation."""
    encoded = value.encode("utf-8", errors="replace")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return value, False, original_bytes
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True, original_bytes


def redact(value: Any) -> Any:
    """Recursively remove secrets and terminal-control sequences from log data."""
    if isinstance(value, Mapping):
        return {
            _clean_string(str(key)): "[REDACTED]" if SENSITIVE_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _clean_string(TOKEN_VALUE.sub("[REDACTED]", value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(repr(value))


def log_event(logger: logging.Logger, level: int, event: str, message: str, **fields: Any) -> None:
    """Log a structured event while preventing unsafe fields from reaching handlers."""
    logger.log(level, redact(message), extra={"event_name": redact(event), "event_fields": redact(fields)})


class JsonLineFormatter(logging.Formatter):
    """Format records as one ANSI-free, safe JSON object per line."""

    def __init__(self, *, run_id: str | None = None, event_max_bytes: int = 65536) -> None:
        super().__init__()
        self.run_id = run_id
        self.event_max_bytes = event_max_bytes

    def format(self, record: logging.LogRecord) -> str:
        fields = _truncate_fields(redact(getattr(record, "event_fields", {})), self.event_max_bytes)
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "event": redact(getattr(record, "event_name", "log.message")),
            "message": redact(record.getMessage()),
            "run_id": self.run_id,
            "request_id": _request_id.get(),
            "conversation_id": _conversation_id.get(),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        elif record.exc_text:
            payload["exception"] = redact(record.exc_text)
        if isinstance(fields, Mapping):
            for key, value in fields.items():
                if key not in payload:
                    payload[key] = value
        return json.dumps(redact(payload), ensure_ascii=False, separators=(",", ":"), default=str)


class ConsoleFormatter(logging.Formatter):
    """Format a compact, human-readable and safe console line."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        source = f"{record.name.rsplit('.', 1)[-1]}:{record.lineno}"
        request_id = _request_id.get()
        short_request_id = str(request_id)[:8] if request_id else "-"
        message = redact(record.getMessage())
        line = f"{timestamp} {record.levelname:<8} {source} [{short_request_id}] {message}"
        if record.exc_info:
            line = f"{line}\n{redact(self.formatException(record.exc_info))}"
        elif record.exc_text:
            line = f"{line}\n{redact(record.exc_text)}"
        return _clean_string(line)


def _settings_from_mapping(values: Mapping[str, str], warning_sink: Callable[[str], None]) -> LoggingSettings:
    defaults = LoggingSettings()
    return LoggingSettings(
        level=_parse_level(values, "LOG_LEVEL", defaults.level, warning_sink),
        backend_level=_parse_level(values, "LOG_BACKEND_LEVEL", defaults.backend_level, warning_sink),
        uvicorn_level=_parse_level(values, "LOG_UVICORN_LEVEL", defaults.uvicorn_level, warning_sink),
        vite_level=_parse_level(values, "LOG_VITE_LEVEL", defaults.vite_level, warning_sink),
        browser_level=_parse_level(values, "LOG_BROWSER_LEVEL", defaults.browser_level, warning_sink),
        log_dir=_parse_path(values, "LOG_DIR", defaults.log_dir, warning_sink),
        max_bytes=_parse_positive_int(values, "LOG_MAX_BYTES", defaults.max_bytes, warning_sink),
        retention_days=_parse_positive_int(values, "LOG_RETENTION_DAYS", defaults.retention_days, warning_sink),
        total_max_bytes=_parse_positive_int(values, "LOG_TOTAL_MAX_BYTES", defaults.total_max_bytes, warning_sink),
        log_llm_payloads=_parse_bool(values, "LOG_LLM_PAYLOADS", defaults.log_llm_payloads, warning_sink),
        browser_batch_size=_parse_positive_int(values, "LOG_BROWSER_BATCH_SIZE", defaults.browser_batch_size, warning_sink),
        browser_flush_ms=_parse_positive_int(values, "LOG_BROWSER_FLUSH_MS", defaults.browser_flush_ms, warning_sink),
        browser_queue_limit=_parse_positive_int(values, "LOG_BROWSER_QUEUE_LIMIT", defaults.browser_queue_limit, warning_sink),
        event_max_bytes=_parse_positive_int(values, "LOG_EVENT_MAX_BYTES", defaults.event_max_bytes, warning_sink),
    )


def _parse_level(values: Mapping[str, str], name: str, default: str, warning_sink: Callable[[str], None]) -> str:
    value = values.get(name)
    if value is None:
        return default
    normalized = str(value).upper()
    if normalized in VALID_LEVELS:
        return normalized
    _warn_invalid(name, warning_sink)
    return default


def _parse_positive_int(values: Mapping[str, str], name: str, default: int, warning_sink: Callable[[str], None]) -> int:
    value = values.get(name)
    if value is None:
        return default
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        _warn_invalid(name, warning_sink)
        return default
    if parsed <= 0:
        _warn_invalid(name, warning_sink)
        return default
    return parsed


def _parse_bool(values: Mapping[str, str], name: str, default: bool, warning_sink: Callable[[str], None]) -> bool:
    value = values.get(name)
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    _warn_invalid(name, warning_sink)
    return default


def _parse_path(values: Mapping[str, str], name: str, default: Path, warning_sink: Callable[[str], None]) -> Path:
    value = values.get(name)
    if value is None:
        return default
    path = Path(str(value).strip())
    if str(path) in {"", "."}:
        _warn_invalid(name, warning_sink)
        return default
    return path


def _warn_invalid(name: str, warning_sink: Callable[[str], None]) -> None:
    warning_sink(f"Invalid {name}; using default")


def _truncate_fields(value: Any, max_bytes: int) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = str(key)
            if isinstance(item, str):
                shortened, was_truncated, original_bytes = truncate_utf8(item, max_bytes)
                result[safe_key] = shortened
                if was_truncated:
                    result[f"{safe_key}_truncated"] = True
                    result[f"{safe_key}_original_bytes"] = original_bytes
            else:
                result[safe_key] = _truncate_fields(item, max_bytes)
        return result
    if isinstance(value, list):
        return [_truncate_fields(item, max_bytes) for item in value]
    return value


def _clean_string(value: str) -> str:
    return ANSI_ESCAPE.sub("", value)
