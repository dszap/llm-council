"""Safe structured logging primitives for the LLM Council backend."""

from __future__ import annotations

import contextvars
import fcntl
import json
import logging
import math
import os
import re
import shutil
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 14
DEFAULT_TOTAL_MAX_BYTES = 500 * 1024 * 1024
VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
SENSITIVE_KEYS = re.compile(
    r"(?:authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|password|secret|token|api(?:[_-]|\s)*key)",
    re.IGNORECASE,
)
TOKEN_VALUE = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-(?:or-)?[a-z0-9_-]*[a-z0-9_-]{8,}|eyJ[a-z0-9_-]{10,}\.[a-z0-9_-]+\.[a-z0-9_-]+)"
)
INLINE_CREDENTIAL_VALUE = re.compile(
    r"""(?ix)
    (?P<prefix>
        \b(?:authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|password|secret|token|api(?:[_-]|\s)*key)\b
        \s*(?:[:=]\s*|\s+)(?:(?:basic|bearer)\s+)?
    )
    (?P<secret>"[^"]*"|'[^']*'|[^\s,;]+)
    """
)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_request_id = contextvars.ContextVar("request_id", default=None)
_conversation_id = contextvars.ContextVar("conversation_id", default=None)

ACTIVE_LOG_FILES = {
    "backend.jsonl",
    "uvicorn.jsonl",
    "vite.jsonl",
    "browser.jsonl",
}


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
class RunContext:
    run_id: str
    run_dir: Path
    latest_link: Path


@dataclass(frozen=True)
class CleanupAction:
    path: str
    reason: str
    bytes_removed: int


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
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(repr(value))


def log_event(logger: logging.Logger, level: int, event: str, message: str, **fields: Any) -> None:
    """Log a structured event while preventing unsafe fields from reaching handlers."""
    logger.log(level, redact(message), extra={"event_name": redact(event), "event_fields": redact(fields)})


class JsonLineFormatter(logging.Formatter):
    """Format records as one ANSI-free, safe JSON object per line."""

    def __init__(
        self,
        *,
        source: str | None = None,
        run_id: str | None = None,
        event_max_bytes: int = 65536,
    ) -> None:
        super().__init__()
        self.source = source
        self.run_id = run_id
        self.event_max_bytes = event_max_bytes

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "source": self.source,
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
        fields = redact(getattr(record, "event_fields", {}))
        if isinstance(fields, Mapping):
            for key, value in fields.items():
                if key not in payload:
                    payload[key] = value
        safe_payload = _normalize_non_finite(_truncate_fields(redact(payload), self.event_max_bytes))
        return json.dumps(
            safe_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        )


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


class _CleanupRotatingFileHandler(RotatingFileHandler):
    """Run retention after rollover completes and the handler lock is released."""

    def __init__(self, *args: Any, rollover_callback: Callable[[], None], **kwargs: Any) -> None:
        self._rollover_callback = rollover_callback
        self._did_rollover = False
        super().__init__(*args, **kwargs)

    def doRollover(self) -> None:
        super().doRollover()
        self._did_rollover = True

    def handle(self, record: logging.LogRecord) -> bool:
        handled = self.filter(record)
        did_rollover = False
        if handled:
            self.acquire()
            try:
                self._did_rollover = False
                self.emit(record)
                did_rollover = self._did_rollover
            finally:
                self._did_rollover = False
                self.release()
            if did_rollover:
                try:
                    self._rollover_callback()
                except Exception:
                    self.handleError(record)
        return handled


@contextmanager
def retention_lock(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    lock_path = log_dir / ".retention.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def create_run_context(settings: LoggingSettings, now: datetime | None = None) -> RunContext:
    current = now or datetime.now(timezone.utc)
    run_id = current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    runs_dir = settings.log_dir / "runs"
    run_dir = runs_dir / run_id
    suffix = 1
    while run_dir.exists():
        run_dir = runs_dir / f"{run_id}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True)
    latest = settings.log_dir / "latest"
    temporary = settings.log_dir / ".latest.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(run_dir.relative_to(settings.log_dir), target_is_directory=True)
    os.replace(temporary, latest)
    return RunContext(run_dir.name, run_dir, latest)


def configure_source_logger(
    name: str,
    source: str,
    path: Path,
    level: str,
    run_id: str,
    settings: LoggingSettings,
    include_console: bool,
) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(level)
    logger.propagate = False

    file_handler = _CleanupRotatingFileHandler(
        path,
        maxBytes=settings.max_bytes,
        backupCount=100,
        encoding="utf-8",
        delay=True,
        rollover_callback=lambda: cleanup_logs(settings, path.parent),
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(
        JsonLineFormatter(
            source=source,
            run_id=run_id,
            event_max_bytes=settings.event_max_bytes,
        )
    )
    logger.addHandler(file_handler)

    if include_console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(ConsoleFormatter())
        logger.addHandler(console_handler)
    return logger


def cleanup_logs(
    settings: LoggingSettings,
    current_run_dir: Path,
    now: datetime | None = None,
) -> list[CleanupAction]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    actions: list[CleanupAction] = []
    with retention_lock(settings.log_dir):
        runs_dir = settings.log_dir / "runs"
        if not runs_dir.is_dir():
            return actions

        protected_runs = {_absolute_path(current_run_dir)}
        latest = settings.log_dir / "latest"
        if latest.is_symlink():
            try:
                protected_runs.add(_absolute_path(latest.resolve(strict=True)))
            except FileNotFoundError:
                pass

        completed_runs = _completed_run_dirs(runs_dir, protected_runs)
        cutoff = current - timedelta(days=settings.retention_days)
        for run_dir in completed_runs:
            if _run_timestamp(run_dir) >= cutoff:
                continue
            actions.append(_remove_run(run_dir, "expired"))

        total_bytes = _tree_size(runs_dir)
        completed_runs = _completed_run_dirs(runs_dir, protected_runs)
        for run_dir in completed_runs:
            if total_bytes <= settings.total_max_bytes:
                break
            action = _remove_run(run_dir, "size_cap")
            actions.append(action)
            total_bytes -= action.bytes_removed

        active_segments = _active_rotated_segments(current_run_dir)
        for segment in active_segments:
            if total_bytes <= settings.total_max_bytes:
                break
            bytes_removed = _tree_size(segment)
            try:
                segment.unlink()
            except FileNotFoundError:
                continue
            actions.append(
                CleanupAction(
                    path=str(segment),
                    reason="active_segment_size_cap",
                    bytes_removed=bytes_removed,
                )
            )
            total_bytes -= bytes_removed
    return actions


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _completed_run_dirs(runs_dir: Path, protected_runs: set[Path]) -> list[Path]:
    run_dirs = [
        path
        for path in runs_dir.iterdir()
        if not path.is_symlink()
        and path.is_dir()
        and _absolute_path(path) not in protected_runs
    ]
    return sorted(run_dirs, key=lambda path: (_run_timestamp(path), path.name))


def _run_timestamp(run_dir: Path) -> datetime:
    try:
        return datetime.strptime(run_dir.name[:20], "%Y-%m-%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return datetime.fromtimestamp(
            run_dir.stat(follow_symlinks=False).st_mtime,
            tz=timezone.utc,
        )


def _remove_run(run_dir: Path, reason: str) -> CleanupAction:
    bytes_removed = _tree_size(run_dir)
    shutil.rmtree(run_dir)
    return CleanupAction(str(run_dir), reason, bytes_removed)


def _tree_size(path: Path) -> int:
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat(follow_symlinks=False).st_size
        if not path.is_dir():
            return 0
        return sum(_tree_size(child) for child in path.iterdir())
    except FileNotFoundError:
        return 0


def _active_rotated_segments(current_run_dir: Path) -> list[Path]:
    segments: list[Path] = []
    if not current_run_dir.is_dir() or current_run_dir.is_symlink():
        return segments
    for path in current_run_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        for base_name in ACTIVE_LOG_FILES:
            prefix = f"{base_name}."
            if path.name.startswith(prefix) and path.name[len(prefix) :].isdigit():
                segments.append(path)
                break
    return sorted(
        segments,
        key=lambda path: (path.stat(follow_symlinks=False).st_mtime_ns, path.name),
    )


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
        log_llm_payloads=_parse_payload_logging(values, "LOG_LLM_PAYLOADS", warning_sink),
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


def _parse_payload_logging(values: Mapping[str, str], name: str, warning_sink: Callable[[str], None]) -> bool:
    value = values.get(name)
    if value is None:
        return False
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized != "false":
        _warn_invalid(name, warning_sink)
    return False


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
        metadata: list[list[Any]] = []
        for key, item in value.items():
            safe_key, key_was_truncated, key_original_bytes = truncate_utf8(
                _clean_string(str(key)), max_bytes
            )
            shortened, was_truncated, original_bytes = _truncate_value(item, max_bytes)
            result[safe_key] = shortened
            if key_was_truncated:
                metadata.append([safe_key, 0, key_original_bytes])
            if was_truncated:
                truncated_key = f"{safe_key}_truncated"
                original_bytes_key = f"{safe_key}_original_bytes"
                if (
                    len(truncated_key.encode("utf-8")) <= max_bytes
                    and len(original_bytes_key.encode("utf-8")) <= max_bytes
                ):
                    result[truncated_key] = True
                    result[original_bytes_key] = original_bytes
                else:
                    metadata.append([safe_key, 1, original_bytes])
        if metadata:
            result["_"] = metadata
        return result
    if isinstance(value, list):
        return [_truncate_value(item, max_bytes)[0] for item in value]
    return value


def _truncate_value(value: Any, max_bytes: int) -> tuple[Any, bool, int | None]:
    if isinstance(value, str):
        return truncate_utf8(value, max_bytes)
    if isinstance(value, Mapping):
        return _truncate_fields(value, max_bytes), False, None
    if isinstance(value, list):
        items = []
        was_truncated = False
        original_bytes = 0
        for item in value:
            shortened, item_was_truncated, item_original_bytes = _truncate_value(item, max_bytes)
            items.append(shortened)
            was_truncated = was_truncated or item_was_truncated
            if item_original_bytes is not None:
                original_bytes += item_original_bytes
        return items, was_truncated, original_bytes if was_truncated else None
    return value, False, None


def _normalize_non_finite(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize_non_finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_non_finite(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _redact_string(value: str) -> str:
    without_inline_credentials = INLINE_CREDENTIAL_VALUE.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]", value
    )
    return _clean_string(TOKEN_VALUE.sub("[REDACTED]", without_inline_credentials))


def _clean_string(value: str) -> str:
    return ANSI_ESCAPE.sub("", value)
