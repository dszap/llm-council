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
import uuid
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
# These maxima are forwarded to the browser and match frontend/src/logger.js.
BROWSER_BATCH_SIZE_MAX = 100
BROWSER_FLUSH_MS_MAX = 60_000
BROWSER_QUEUE_LIMIT_MAX = 1_000
EVENT_MAX_BYTES_MAX = 65_536
VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
SENSITIVE_KEYS = re.compile(
    r"(?:authorization|proxyauthorization|cookie|setcookie|password|secret|token|apikey)",
    re.IGNORECASE,
)
TOKEN_VALUE = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-(?:or-)?[a-z0-9_-]*[a-z0-9_-]{8,}|eyJ[a-z0-9_-]{10,}\.[a-z0-9_-]+\.[a-z0-9_-]+)"
)
INLINE_CREDENTIAL_VALUE = re.compile(
    r"""(?ix)
    (?P<prefix>
        \b(?:authorization|proxy[-_ ]?authorization|cookie|set[-_ ]?cookie|password|secret|token|api[-_ ]?key)\b
        \s*(?:[:=]\s*|\s+)(?:(?:basic|bearer)\s+)?
    )
    (?P<secret>"[^"]*"|'[^']*'|[^\s,;]+)
    """
)
COOKIE_HEADER_VALUE = re.compile(
    r"(?im)(?P<prefix>\b(?:cookie|set[-_ ]?cookie)\b\s*(?:[:=]\s*|\s+))(?P<secret>[^\r\n]*)"
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
    event_max_bytes: int = EVENT_MAX_BYTES_MAX

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
            _clean_string(str(key)): "[REDACTED]" if _is_sensitive_key(str(key)) else redact(item)
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
        if event_max_bytes < 2:
            raise ValueError("event_max_bytes must be at least 2")
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
        safe_payload = _fit_payload(safe_payload, self.event_max_bytes)
        return _serialize_payload(safe_payload)


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

    def __init__(
        self,
        *args: Any,
        retention_log_dir: Path,
        rollover_callback: Callable[[], None],
        **kwargs: Any,
    ) -> None:
        self._retention_log_dir = retention_log_dir
        self._rollover_callback = rollover_callback
        self._did_rollover = False
        super().__init__(*args, **kwargs)

    def doRollover(self) -> None:
        with retention_lock(self._retention_log_dir):
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
    base_run_id = current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    runs_dir = settings.log_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    suffix = 0
    while True:
        directory_name = base_run_id if suffix == 0 else f"{base_run_id}-{suffix}"
        run_dir = runs_dir / directory_name
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            suffix += 1
    run_id = directory_name
    latest = settings.log_dir / "latest"
    temporary = settings.log_dir / f".latest.{uuid.uuid4().hex}.tmp"
    try:
        temporary.symlink_to(
            run_dir.relative_to(settings.log_dir),
            target_is_directory=True,
        )
        os.replace(temporary, latest)
    finally:
        temporary.unlink(missing_ok=True)
    return RunContext(run_id, run_dir, latest)


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
        retention_log_dir=settings.log_dir,
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

        active_run_dir = _path_identity(current_run_dir)
        protected_runs = {active_run_dir}
        latest = settings.log_dir / "latest"
        if latest.is_symlink():
            try:
                protected_runs.add(_path_identity(latest.resolve(strict=True)))
            except FileNotFoundError:
                pass
        protected_runs.update(_active_run_identities(runs_dir))

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

        active_segments = _active_rotated_segments(active_run_dir)
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
        if total_bytes > settings.total_max_bytes:
            actions.append(
                CleanupAction(
                    path=str(_path_identity(runs_dir)),
                    reason="size_cap_hard_floor",
                    bytes_removed=0,
                )
            )
    return actions


def _path_identity(path: Path) -> Path:
    return path.resolve(strict=False)


def _completed_run_dirs(runs_dir: Path, protected_runs: set[Path]) -> list[Path]:
    run_dirs = [
        path
        for path in runs_dir.iterdir()
        if not path.is_symlink()
        and path.is_dir()
        and _path_identity(path) not in protected_runs
    ]
    return sorted(run_dirs, key=lambda path: (_run_timestamp(path), path.name))


def _active_run_identities(runs_dir: Path) -> set[Path]:
    """Return runs whose supervisor manifest has not recorded termination."""
    active: set[Path] = set()
    for run_dir in runs_dir.iterdir():
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        manifest_path = run_dir / "manifest.json"
        try:
            with manifest_path.open(encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(manifest, Mapping):
            continue
        if manifest.get("status") in {"starting", "running"} and not manifest.get("ended_at"):
            active.add(_path_identity(run_dir))
    return active


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
    general_level = _parse_level(values, "LOG_LEVEL", defaults.level, warning_sink)
    general_value = values.get("LOG_LEVEL")
    general_override = (
        general_level
        if general_value is not None and str(general_value).upper() in VALID_LEVELS
        else None
    )
    return LoggingSettings(
        level=general_level,
        backend_level=_parse_level(
            values,
            "LOG_BACKEND_LEVEL",
            defaults.backend_level if general_override is None else general_override,
            warning_sink,
        ),
        uvicorn_level=_parse_level(
            values,
            "LOG_UVICORN_LEVEL",
            defaults.uvicorn_level if general_override is None else general_override,
            warning_sink,
        ),
        vite_level=_parse_level(
            values,
            "LOG_VITE_LEVEL",
            defaults.vite_level if general_override is None else general_override,
            warning_sink,
        ),
        browser_level=_parse_level(
            values,
            "LOG_BROWSER_LEVEL",
            defaults.browser_level if general_override is None else general_override,
            warning_sink,
        ),
        log_dir=_parse_path(values, "LOG_DIR", defaults.log_dir, warning_sink),
        max_bytes=_parse_positive_int(values, "LOG_MAX_BYTES", defaults.max_bytes, warning_sink),
        retention_days=_parse_positive_int(values, "LOG_RETENTION_DAYS", defaults.retention_days, warning_sink),
        total_max_bytes=_parse_positive_int(values, "LOG_TOTAL_MAX_BYTES", defaults.total_max_bytes, warning_sink),
        log_llm_payloads=_parse_payload_logging(values, "LOG_LLM_PAYLOADS", warning_sink),
        browser_batch_size=_parse_positive_int(
            values,
            "LOG_BROWSER_BATCH_SIZE",
            defaults.browser_batch_size,
            warning_sink,
            maximum=BROWSER_BATCH_SIZE_MAX,
        ),
        browser_flush_ms=_parse_positive_int(
            values,
            "LOG_BROWSER_FLUSH_MS",
            defaults.browser_flush_ms,
            warning_sink,
            maximum=BROWSER_FLUSH_MS_MAX,
        ),
        browser_queue_limit=_parse_positive_int(
            values,
            "LOG_BROWSER_QUEUE_LIMIT",
            defaults.browser_queue_limit,
            warning_sink,
            maximum=BROWSER_QUEUE_LIMIT_MAX,
        ),
        event_max_bytes=_parse_positive_int(
            values,
            "LOG_EVENT_MAX_BYTES",
            defaults.event_max_bytes,
            warning_sink,
            minimum=2,
            maximum=EVENT_MAX_BYTES_MAX,
        ),
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


def _parse_positive_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    warning_sink: Callable[[str], None],
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    value = values.get(name)
    if value is None:
        return default
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        _warn_invalid(name, warning_sink)
        return default
    if parsed < minimum or (maximum is not None and parsed > maximum):
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


def _is_sensitive_key(key: str) -> bool:
    """Match sensitive field names independent of separators or casing."""
    canonical = re.sub(r"[^a-z0-9]", "", key.lower())
    return bool(SENSITIVE_KEYS.search(canonical))


def _serialize_payload(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _truncate_strings(value: Any, max_bytes: int) -> Any:
    if isinstance(value, Mapping):
        return {key: _truncate_strings(item, max_bytes) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_strings(item, max_bytes) for item in value]
    if isinstance(value, str):
        return truncate_utf8(value, max_bytes)[0]
    return value


def _fit_payload(value: Mapping[str, Any], max_bytes: int) -> dict[str, Any]:
    """Fit the complete compact JSON payload, dropping least useful fields first."""
    candidate = dict(value)
    optional = {
        key: item
        for key, item in candidate.items()
        if key not in {
            "timestamp", "level", "source", "logger", "event", "message",
            "run_id", "request_id", "conversation_id",
        }
    }
    if len(_serialize_payload(candidate).encode("utf-8")) <= max_bytes:
        return candidate

    # Structured event fields and exception text are optional when the complete
    # record would exceed the configured bound.  Keep the standard envelope.
    envelope_keys = {
        "timestamp", "level", "source", "logger", "event", "message",
        "run_id", "request_id", "conversation_id",
    }
    candidate = {key: item for key, item in candidate.items() if key in envelope_keys}

    # A shared string budget guarantees that the aggregate payload, rather than
    # each individual value, is bounded.  Binary search keeps useful prefixes.
    low, high = 0, max_bytes
    best: dict[str, Any] | None = None
    while low <= high:
        middle = (low + high) // 2
        compacted = _truncate_strings(candidate, middle)
        if len(_serialize_payload(compacted).encode("utf-8")) <= max_bytes:
            best = compacted
            low = middle + 1
        else:
            high = middle - 1
    if best is not None:
        return best

    # If the envelope itself cannot fit, retain compact structured metadata
    # (including truncation markers) when that is the most useful representation.
    low, high = 0, max_bytes
    optional_best: dict[str, Any] | None = None
    while low <= high:
        middle = (low + high) // 2
        compacted = _truncate_strings(optional, middle)
        if len(_serialize_payload(compacted).encode("utf-8")) <= max_bytes:
            optional_best = compacted
            low = middle + 1
        else:
            high = middle - 1
    if optional_best is not None and optional_best:
        return optional_best

    # Very small limits cannot hold the complete envelope. Drop optional
    # correlation/source fields while retaining a useful message when possible.
    for key in ("conversation_id", "request_id", "run_id", "logger", "source", "level", "event"):
        candidate.pop(key, None)
        compacted = _truncate_strings(candidate, max_bytes)
        if len(_serialize_payload(compacted).encode("utf-8")) <= max_bytes:
            return compacted

    # Retain as much of the message as the compact object shape permits.
    message = candidate.get("message")
    if isinstance(message, str):
        low, high = 0, max_bytes
        best_message: dict[str, Any] | None = None
        while low <= high:
            middle = (low + high) // 2
            compacted = {"message": truncate_utf8(message, middle)[0]}
            if len(_serialize_payload(compacted).encode("utf-8")) <= max_bytes:
                best_message = compacted
                low = middle + 1
            else:
                high = middle - 1
        if best_message is not None:
            return best_message

    fallback = {"message": "[truncated]"}
    if len(_serialize_payload(fallback).encode("utf-8")) <= max_bytes:
        return fallback
    empty_payload: dict[str, Any] = {}
    if len(_serialize_payload(empty_payload).encode("utf-8")) <= max_bytes:
        return empty_payload
    raise ValueError("max_bytes cannot contain a JSON object")


def _redact_string(value: str) -> str:
    without_cookie_headers = COOKIE_HEADER_VALUE.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]", value
    )
    without_inline_credentials = INLINE_CREDENTIAL_VALUE.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]", without_cookie_headers
    )
    return _clean_string(TOKEN_VALUE.sub("[REDACTED]", without_inline_credentials))


def _clean_string(value: str) -> str:
    return ANSI_ESCAPE.sub("", value)
