# Agent-Friendly Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable, structured, correlated local logging for backend, Uvicorn, Vite, and browser sources while preserving readable terminal output and enforcing rotation and retention.

**Architecture:** `start.sh` delegates to a Python supervisor that creates a timestamped run, starts both services, captures Vite output, owns the manifest, and coordinates cleanup. The backend uses Python standard-library logging for application, Uvicorn, and browser-ingestion JSONL streams; the frontend batches important browser events to a bounded local endpoint.

**Tech Stack:** Python 3.10, standard-library `logging`/`subprocess`/`contextvars`/`fcntl`, FastAPI/Pydantic, React 19, Vite 7, Node's built-in test runner, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-26-agent-friendly-logging-design.md`

## Global Constraints

- Show backend application, Uvicorn, Vite, and forwarded browser events in the terminal used to run `start.sh`.
- Write valid, ANSI-free JSONL to separate `backend.jsonl`, `uvicorn.jsonl`, `vite.jsonl`, and `browser.jsonl` files.
- Use UTC run IDs formatted as `YYYY-MM-DDTHHMMSSZ` and expose the newest run through an atomic `logs/latest` symlink.
- Rotate each source at exactly 10 MiB by default, retain runs for exactly 14 days, and cap aggregate log storage at exactly 500 MiB.
- Never delete active base files, the active manifest, the `latest` target, or the cleanup lock.
- Default LLM logging to metadata only; payload logging requires `LOG_LLM_PAYLOADS=true`.
- Redact authorization headers, cookies, API keys, bearer tokens, passwords, secrets, and token-shaped values in every mode.
- Browser logging defaults to `WARNING`, remains bounded and non-blocking, and never affects application behavior.
- Add no runtime logging dependency and no frontend test framework dependency.
- Automated tests must not make live OpenRouter requests or consume credits.
- Preserve the existing `.env` key value without printing it or staging `.env`.

---

## File Structure

### New files

- `backend/logging_config.py` — settings, context binding, JSON formatting, redaction, file handlers, run discovery, rotation, and retention.
- `backend/dev_runner.py` — run lifecycle, manifest, backend/Vite subprocess supervision, terminal mirroring, and shutdown.
- `frontend/src/logger.js` — browser capture, sanitization, queueing, batching, and transport.
- `frontend/src/logger.test.js` — Node-native browser logger tests.
- `tests/__init__.py` — Python test package marker.
- `tests/test_logging_config.py` — settings, formatting, context, redaction, and truncation tests.
- `tests/test_log_retention.py` — run creation, rotation, lock, age, and size-retention tests.
- `tests/test_http_logging.py` — request correlation and browser-ingestion endpoint tests.
- `tests/test_domain_logging.py` — mocked OpenRouter and council-stage event tests.
- `tests/test_dev_runner.py` — supervisor lifecycle and manifest tests.
- `tests/test_logging_smoke.py` — opt-in real-process smoke test.

### Modified files

- `backend/main.py` — initialize logging, add correlation middleware, bind conversation context, add browser log ingestion, and configure Uvicorn.
- `backend/openrouter.py` — replace `print` with structured, timed, redacted model-request events.
- `backend/council.py` — add stage lifecycle and fallback events.
- `backend/config.py` — keep application configuration separate; expose no secrets to manifest or browser settings.
- `start.sh` — become a repository-root-aware supervisor wrapper.
- `frontend/src/api.js` — export the API base for browser logging.
- `frontend/src/main.jsx` — install browser logging once and dispose it during hot reload.
- `frontend/src/App.jsx` — fix hook ordering with stable callbacks so lint passes.
- `frontend/src/components/Sidebar.jsx` — remove unused React imports so lint passes.
- `frontend/package.json` — add the Node-native `test` script.
- `.gitignore` — ignore `logs/`.
- `.env` — add detailed logging defaults without touching the key; this remains ignored and uncommitted.
- `README.md` — document log discovery, payload safety, configuration, and cleanup.

---

### Task 1: Structured Logging Core and Redaction

**Files:**
- Create: `backend/logging_config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_logging_config.py`

**Interfaces:**
- Consumes: process environment, a filesystem path, and standard `logging.LogRecord` objects.
- Produces: `LoggingSettings.from_env(env: Mapping[str, str]) -> LoggingSettings`, `LoggingSettings.to_safe_dict() -> dict[str, Any]`, `LogContextTokens`, `bind_log_context(request_id: str | None, conversation_id: str | None) -> LogContextTokens`, `reset_log_context(tokens: LogContextTokens) -> None`, `redact(value: Any) -> Any`, `truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool, int]`, `log_event(logger, level, event, message, **fields)`, `JsonLineFormatter`, and `ConsoleFormatter`.

- [ ] **Step 1: Write failing settings, redaction, context, and formatter tests**

Create `tests/__init__.py` as an empty file. In `tests/test_logging_config.py`, add tests with fixed time and in-memory streams:

```python
import io
import json
import logging
import unittest
from pathlib import Path

from backend.logging_config import (
    JsonLineFormatter,
    LoggingSettings,
    bind_log_context,
    log_event,
    redact,
    reset_log_context,
    truncate_utf8,
)


class LoggingSettingsTests(unittest.TestCase):
    def test_defaults_match_specification(self):
        settings = LoggingSettings.from_env({})
        self.assertEqual(settings.level, "INFO")
        self.assertEqual(settings.browser_level, "WARNING")
        self.assertEqual(settings.max_bytes, 10 * 1024 * 1024)
        self.assertEqual(settings.retention_days, 14)
        self.assertEqual(settings.total_max_bytes, 500 * 1024 * 1024)
        self.assertFalse(settings.log_llm_payloads)
        self.assertEqual(settings.log_dir, Path("logs"))

    def test_invalid_values_fall_back_and_report_warnings(self):
        warnings = []
        settings = LoggingSettings.from_env(
            {"LOG_MAX_BYTES": "zero", "LOG_BROWSER_LEVEL": "LOUD"},
            warning_sink=warnings.append,
        )
        self.assertEqual(settings.max_bytes, 10 * 1024 * 1024)
        self.assertEqual(settings.browser_level, "WARNING")
        self.assertEqual(len(warnings), 2)


class RedactionTests(unittest.TestCase):
    def test_redacts_sensitive_keys_and_token_shaped_values(self):
        value = {
            "Authorization": "Bearer secret-value",
            "nested": {"api_key": "sk-or-v1-secret", "safe": "visible"},
            "text": "token sk-or-v1-abcdefghijklmnopqrstuvwxyz",
        }
        result = redact(value)
        self.assertEqual(result["Authorization"], "[REDACTED]")
        self.assertEqual(result["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(result["nested"]["safe"], "visible")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", result["text"])

    def test_utf8_truncation_reports_original_size(self):
        value, truncated, original_bytes = truncate_utf8("á" * 10, 8)
        self.assertTrue(truncated)
        self.assertLessEqual(len(value.encode("utf-8")), 8)
        self.assertEqual(original_bytes, 20)


class JsonFormatterTests(unittest.TestCase):
    def test_event_contains_context_and_no_ansi(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter(run_id="2026-08-26T220712Z"))
        logger = logging.getLogger("test.logging.core")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        tokens = bind_log_context(request_id="request-1", conversation_id="conversation-1")
        try:
            log_event(logger, logging.INFO, "test.completed", "Done", answer=42)
        finally:
            reset_log_context(tokens)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["event"], "test.completed")
        self.assertEqual(payload["request_id"], "request-1")
        self.assertEqual(payload["conversation_id"], "conversation-1")
        self.assertEqual(payload["answer"], 42)
        self.assertNotIn("\x1b", stream.getvalue())
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
uv run python -m unittest tests.test_logging_config -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'backend.logging_config'`.

- [ ] **Step 3: Implement immutable settings, context binding, sanitization, and formatters**

Create `backend/logging_config.py` with these public types and functions:

```python
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 14
DEFAULT_TOTAL_MAX_BYTES = 500 * 1024 * 1024
VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
SENSITIVE_KEYS = re.compile(
    r"(?:authorization|proxy_authorization|cookie|set_cookie|password|secret|token|api_key)",
    re.IGNORECASE,
)
TOKEN_VALUE = re.compile(r"(?i)(?:bearer\s+|sk-(?:or-)?[a-z0-9-]*)([a-z0-9_-]{12,})")

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
    return LogContextTokens(_request_id.set(request_id), _conversation_id.set(conversation_id))


def reset_log_context(tokens: LogContextTokens) -> None:
    _request_id.reset(tokens.request_id)
    _conversation_id.reset(tokens.conversation_id)


def truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool, int]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value, False, len(encoded)
    shortened = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return shortened, True, len(encoded)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return TOKEN_VALUE.sub(lambda match: match.group(0)[:8] + "[REDACTED]", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)


def log_event(logger: logging.Logger, level: int, event: str, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"event_name": event, "event_fields": redact(fields)})
```

Implement `_settings_from_mapping` with explicit integer, boolean, level, and path parsers. Reject non-positive numeric values; emit one warning per invalid value; never echo raw environment values in warnings. Implement `JsonLineFormatter.format()` to emit the specified common envelope with UTC RFC 3339 milliseconds, context variables, exception data, and sanitized `event_fields`. Implement `ConsoleFormatter.format()` with UTC time, level, short source, eight-character request ID, and exception text. Enforce `event_max_bytes` by truncating oversized string fields before JSON serialization and recording `<field>_truncated` and `<field>_original_bytes`.

- [ ] **Step 4: Run focused tests and verify they pass**

Run:

```bash
uv run python -m unittest tests.test_logging_config -v
```

Expected: all tests pass with no output containing the marker secret.

- [ ] **Step 5: Commit the structured logging core**

```bash
git add backend/logging_config.py tests/__init__.py tests/test_logging_config.py
git commit -m "feature: add structured logging core"
```

---

### Task 2: Run Directories, Rotation, and Retention

**Files:**
- Modify: `backend/logging_config.py`
- Create: `tests/test_log_retention.py`

**Interfaces:**
- Consumes: `LoggingSettings` from Task 1 and a root log directory.
- Produces: `RunContext(run_id, run_dir, latest_link)`, `CleanupAction`, `create_run_context(settings, now)`, `configure_source_logger(name, source, path, level, run_id, settings, include_console)`, `cleanup_logs(settings, current_run_dir, now)`, and `retention_lock(log_dir)`.

- [ ] **Step 1: Write failing run-layout, rotation, and cleanup tests**

Create `tests/test_log_retention.py` with deterministic temporary directories:

```python
import json
import logging
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.logging_config import (
    LoggingSettings,
    cleanup_logs,
    configure_source_logger,
    create_run_context,
    log_event,
)


class RunContextTests(unittest.TestCase):
    def test_creates_timestamped_run_and_atomic_latest_link(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(LoggingSettings(), log_dir=Path(directory))
            context = create_run_context(
                settings,
                now=datetime(2026, 8, 26, 22, 7, 12, tzinfo=timezone.utc),
            )
            self.assertEqual(context.run_id, "2026-08-26T220712Z")
            self.assertTrue(context.run_dir.is_dir())
            self.assertEqual(context.latest_link.resolve(), context.run_dir.resolve())


class RotationTests(unittest.TestCase):
    def test_rotates_without_ansi_and_keeps_active_file(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(LoggingSettings(), log_dir=Path(directory), max_bytes=256)
            context = create_run_context(settings)
            logger = configure_source_logger(
                name="test.rotation",
                source="backend",
                path=context.run_dir / "backend.jsonl",
                level="INFO",
                run_id=context.run_id,
                settings=settings,
                include_console=False,
            )
            for index in range(20):
                log_event(logger, logging.INFO, "rotation.line", "x" * 80, index=index)
            for handler in logger.handlers:
                handler.flush()
            self.assertTrue((context.run_dir / "backend.jsonl").exists())
            self.assertTrue((context.run_dir / "backend.jsonl.1").exists())
            for path in context.run_dir.glob("backend.jsonl*"):
                for line in path.read_text().splitlines():
                    json.loads(line)
                    self.assertNotIn("\x1b", line)


class RetentionTests(unittest.TestCase):
    def test_deletes_expired_then_oldest_completed_runs_and_preserves_active_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                LoggingSettings(),
                log_dir=root,
                retention_days=14,
                total_max_bytes=350,
            )
            now = datetime(2026, 8, 26, tzinfo=timezone.utc)
            active = create_run_context(settings, now=now).run_dir
            expired = root / "runs" / "2026-08-01T000000Z"
            old = root / "runs" / "2026-08-20T000000Z"
            expired.mkdir(parents=True)
            old.mkdir(parents=True)
            (expired / "backend.jsonl").write_bytes(b"x" * 300)
            (old / "backend.jsonl").write_bytes(b"x" * 300)
            (active / "backend.jsonl").write_bytes(b"x" * 100)
            actions = cleanup_logs(settings, active, now=now)
            self.assertFalse(expired.exists())
            self.assertFalse(old.exists())
            self.assertTrue((active / "backend.jsonl").exists())
            self.assertTrue(any(action.reason == "expired" for action in actions))
            self.assertTrue(any(action.reason == "size_cap" for action in actions))
```

Add these methods to `RetentionTests`:

```python
    def test_active_run_removes_oldest_segment_but_not_base_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(LoggingSettings(), log_dir=root, total_max_bytes=250)
            active = create_run_context(settings).run_dir
            base = active / "backend.jsonl"
            newest = active / "backend.jsonl.1"
            oldest = active / "backend.jsonl.2"
            base.write_bytes(b"b" * 100)
            newest.write_bytes(b"n" * 100)
            oldest.write_bytes(b"o" * 100)
            os.utime(oldest, (1, 1))
            os.utime(newest, (2, 2))
            actions = cleanup_logs(settings, active)
            self.assertTrue(base.exists())
            self.assertTrue(newest.exists())
            self.assertFalse(oldest.exists())
            self.assertEqual(actions[-1].reason, "active_segment_size_cap")

    def test_cleanup_is_idempotent_after_a_second_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(LoggingSettings(), log_dir=root, retention_days=1)
            active = create_run_context(settings).run_dir
            expired = root / "runs" / "2026-01-01T000000Z"
            expired.mkdir()
            (expired / "backend.jsonl").write_bytes(b"x" * 100)
            first = cleanup_logs(settings, active)
            second = cleanup_logs(settings, active)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertTrue((root / ".retention.lock").exists())
            self.assertTrue(active.exists())
```

Import `multiprocessing` and `retention_lock`, add this module-level worker, then add the method:

```python
def _cleanup_worker(settings, active, started, finished):
    started.set()
    cleanup_logs(settings, active)
    finished.set()


class RetentionLockTests(unittest.TestCase):
    def test_cleanup_waits_for_retention_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(LoggingSettings(), log_dir=root)
            active = create_run_context(settings).run_dir
            context = multiprocessing.get_context("spawn")
            started = context.Event()
            finished = context.Event()
            process = context.Process(
                target=_cleanup_worker,
                args=(settings, active, started, finished),
            )
            with retention_lock(root):
                process.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.is_set())
            self.assertTrue(finished.wait(1))
            process.join(timeout=1)
            self.assertEqual(process.exitcode, 0)
```

- [ ] **Step 2: Run retention tests and verify they fail**

Run:

```bash
uv run python -m unittest tests.test_log_retention -v
```

Expected: import errors for the missing run, handler, and retention interfaces.

- [ ] **Step 3: Implement run context, rotating handlers, locking, and deterministic cleanup**

Add these public records and core functions to `backend/logging_config.py`:

```python
import fcntl
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler


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
```

Use `RotatingFileHandler(path, maxBytes=settings.max_bytes, backupCount=100, encoding="utf-8")`. Attach `JsonLineFormatter`, set `delay=True`, and use a callback-capable subclass that invokes cleanup only after the base rollover finishes and its file lock is released. `configure_source_logger` must clear and close prior handlers for the exact logger, disable propagation, attach one file handler, and optionally attach one console handler.

Implement cleanup exactly in this order under `retention_lock`: expired completed directories, oldest completed directories until under the cap, then oldest rotated active segments until under the cap. Compute sizes without following symlinks. Identify active base files by exact names `backend.jsonl`, `uvicorn.jsonl`, `vite.jsonl`, and `browser.jsonl`. Return actions rather than logging while the lock is held.

- [ ] **Step 4: Run core and retention tests**

Run:

```bash
uv run python -m unittest tests.test_logging_config tests.test_log_retention -v
```

Expected: all tests pass; temporary directories contain no malformed JSONL.

- [ ] **Step 5: Commit rotation and retention**

```bash
git add backend/logging_config.py tests/test_log_retention.py
git commit -m "feature: add log rotation and retention"
```

---

### Task 3: Backend Initialization, Request Correlation, and Browser Ingestion

**Files:**
- Modify: `backend/main.py:1-199`
- Create: `tests/test_http_logging.py`

**Interfaces:**
- Consumes: Task 1/2 logging settings, context, logger factories, `X-Request-ID`, and browser JSON batches.
- Produces: `valid_request_id(value: str | None) -> str`, request state/header correlation, conversation-bound contexts, `BrowserLogEvent`, `BrowserLogBatch`, and `POST /api/logs/browser`.

- [ ] **Step 1: Write failing request and browser endpoint tests**

Create `tests/test_http_logging.py`:

```python
import io
import json
import logging
import unittest

from fastapi.testclient import TestClient

from backend.main import app


class HttpLoggingTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_generates_and_returns_request_id(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            response.headers["x-request-id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )

    def test_preserves_valid_incoming_request_id(self):
        request_id = "28e8f443-7eb8-41e4-8ca6-79689b13d36d"
        response = self.client.get("/", headers={"X-Request-ID": request_id})
        self.assertEqual(response.headers["x-request-id"], request_id)

    def test_browser_batch_is_logged_with_server_owned_source(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(event_name)s|%(message)s"))
        logger = logging.getLogger("llm_council.browser")
        original_handlers = logger.handlers[:]
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        try:
            response = self.client.post(
                "/api/logs/browser",
                headers={"Origin": "http://localhost:5173"},
                json={
                    "events": [
                        {
                            "client_timestamp": "2026-08-26T22:07:12.438Z",
                            "level": "ERROR",
                            "event": "browser.unhandled_error",
                            "message": "boom",
                            "browser_session_id": "session-1",
                            "page": "http://localhost:5173/",
                            "details": {"authorization": "Bearer secret-value"},
                        }
                    ]
                },
            )
        finally:
            logger.handlers = original_handlers
        self.assertEqual(response.status_code, 202)
        self.assertIn("browser.unhandled_error|boom", stream.getvalue())
        self.assertNotIn("secret-value", stream.getvalue())

    def test_rejects_untrusted_origin_and_oversized_batch(self):
        event = {
            "client_timestamp": "2026-08-26T22:07:12.438Z",
            "level": "WARNING",
            "event": "browser.warning",
            "message": "warning",
            "browser_session_id": "session-1",
            "page": "http://localhost:5173/",
        }
        forbidden = self.client.post(
            "/api/logs/browser",
            headers={"Origin": "https://example.com"},
            json={"events": [event]},
        )
        oversized = self.client.post(
            "/api/logs/browser",
            headers={"Origin": "http://localhost:5173"},
            json={"events": [event] * 21},
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(oversized.status_code, 413)
```

Extend the imports with `AsyncMock`, `Mock`, `patch`, `JsonLineFormatter`, and `log_event`, then add these methods:

```python
    def test_conversation_route_binds_request_and_conversation_ids(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter(run_id="test-run"))
        logger = logging.getLogger("llm_council.backend")
        original_handlers = logger.handlers[:]
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        async def fake_council(user_query):
            log_event(logger, logging.INFO, "test.council", "Council called")
            return [], [], {"model": "test/model", "response": "ok"}, {}

        conversation_id = "13c799b4-0d8f-42b9-9b7d-7c2ed3d478d7"
        request_id = "28e8f443-7eb8-41e4-8ca6-79689b13d36d"
        try:
            with patch("backend.main.storage.get_conversation", return_value={"messages": [{}]}), \
                 patch("backend.main.storage.add_user_message"), \
                 patch("backend.main.storage.add_assistant_message"), \
                 patch("backend.main.run_full_council", new=AsyncMock(side_effect=fake_council)):
                response = self.client.post(
                    f"/api/conversations/{conversation_id}/message",
                    headers={"X-Request-ID": request_id},
                    json={"content": "question"},
                )
        finally:
            logger.handlers = original_handlers
        self.assertEqual(response.status_code, 200)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["request_id"], request_id)
        self.assertEqual(payload["conversation_id"], conversation_id)

    def test_stream_generator_rebinds_context_until_completion(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter(run_id="test-run"))
        logger = logging.getLogger("llm_council.backend")
        original_handlers = logger.handlers[:]
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        async def fake_stage1(user_query):
            log_event(logger, logging.INFO, "test.stream.stage1", "Stage called")
            return []

        conversation_id = "13c799b4-0d8f-42b9-9b7d-7c2ed3d478d7"
        request_id = "28e8f443-7eb8-41e4-8ca6-79689b13d36d"
        try:
            with patch("backend.main.storage.get_conversation", return_value={"messages": [{}]}), \
                 patch("backend.main.storage.add_user_message"), \
                 patch("backend.main.storage.add_assistant_message"), \
                 patch("backend.main.stage1_collect_responses", new=AsyncMock(side_effect=fake_stage1)), \
                 patch("backend.main.stage2_collect_rankings", new=AsyncMock(return_value=([], {}))), \
                 patch("backend.main.stage3_synthesize_final", new=AsyncMock(return_value={"model": "test/model", "response": "ok"})):
                with self.client.stream(
                    "POST",
                    f"/api/conversations/{conversation_id}/message/stream",
                    headers={"X-Request-ID": request_id},
                    json={"content": "question"},
                ) as response:
                    body = "".join(response.iter_text())
        finally:
            logger.handlers = original_handlers
        self.assertIn('"type": "complete"', body)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["request_id"], request_id)
        self.assertEqual(payload["conversation_id"], conversation_id)
```

- [ ] **Step 2: Run the HTTP logging tests and verify they fail**

Run:

```bash
uv run python -m unittest tests.test_http_logging -v
```

Expected: missing `X-Request-ID`, a 404 browser endpoint, and failed correlation assertions.

- [ ] **Step 3: Add middleware, schemas, browser ingestion, and startup logging**

In `backend/main.py`:

1. Define bounded Pydantic schemas using `Literal`, `Field`, and `ConfigDict(extra="forbid")`.
2. Add a UUID-validating request-ID helper.
3. Add HTTP middleware that binds request context, times the request, logs completion/failure, writes `request.state.request_id`, returns `X-Request-ID`, and always resets context.
4. Accept `Request` in conversation endpoints and bind `conversation_id` around their work.
5. Inside the SSE generator, re-bind both `request.state.request_id` and the conversation ID for the generator's full lifetime.
6. Add the browser endpoint with exact local-origin validation, count and serialized-size checks, fixed level mapping, server-side redaction, and a `202 {"accepted": count}` response.
7. In the `__main__` block, load settings and the supervisor-provided `LLM_COUNCIL_RUN_DIR`/`LLM_COUNCIL_RUN_ID`, configure backend/browser/Uvicorn loggers, emit `backend.started`, and fall back to console-only logging on filesystem initialization failure.

Configure `uvicorn`, `uvicorn.error`, and `uvicorn.access` to propagate only to the dedicated Uvicorn handlers, and call `uvicorn.run(app, host="0.0.0.0", port=8001, log_config=None, access_log=True)` so Uvicorn does not replace the application configuration. When supervisor variables are absent, create a standalone run context before configuring handlers.

Use this endpoint shape:

```python
@app.post("/api/logs/browser", status_code=202)
async def ingest_browser_logs(batch: BrowserLogBatch, request: Request):
    origin = request.headers.get("origin")
    if origin not in ALLOWED_FRONTEND_ORIGINS:
        raise HTTPException(status_code=403, detail="Origin not allowed")
    settings = LoggingSettings.from_env()
    if len(batch.events) > settings.browser_batch_size:
        raise HTTPException(status_code=413, detail="Browser log batch too large")
    for event in batch.events:
        level = BROWSER_LEVELS[event.level]
        log_event(
            browser_logger,
            level,
            event.event,
            event.message,
            client_timestamp=event.client_timestamp,
            browser_session_id=event.browser_session_id,
            page=event.page,
            details=event.details or {},
        )
    return {"accepted": len(batch.events)}
```

Do not log request bodies or browser headers. Validate body size before logging any event. Keep CORS origins and ingestion origins sourced from the same constant.

- [ ] **Step 4: Run HTTP and core tests**

Run:

```bash
uv run python -m unittest tests.test_logging_config tests.test_log_retention tests.test_http_logging -v
```

Expected: all tests pass, including the fully consumed SSE test.

- [ ] **Step 5: Commit backend correlation and ingestion**

```bash
git add backend/main.py tests/test_http_logging.py
git commit -m "feature: add correlated browser log ingestion"
```

---

### Task 4: OpenRouter and Council Domain Instrumentation

**Files:**
- Modify: `backend/openrouter.py:1-79`
- Modify: `backend/council.py:1-335`
- Create: `tests/test_domain_logging.py`

**Interfaces:**
- Consumes: `log_event`, current request/conversation context, `LoggingSettings.log_llm_payloads`, and existing council/OpenRouter functions.
- Produces: stable events `openrouter.request.started`, `openrouter.request.completed`, `openrouter.request.failed`, `council.stage.started`, `council.stage.completed`, `council.all_models_failed`, and `council.chairman_fallback`.

- [ ] **Step 1: Write failing mocked domain-event tests**

Create `tests/test_domain_logging.py` using `unittest.IsolatedAsyncioTestCase` and `unittest.mock`:

```python
import logging
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend import council, openrouter


class OpenRouterLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_logs_metadata_without_payload_by_default(self):
        response = {
            "choices": [{"message": {"content": "private answer", "reasoning_details": None}}]
        }
        fake_http_response = Mock()
        fake_http_response.raise_for_status.return_value = None
        fake_http_response.json.return_value = response
        fake_client = AsyncMock()
        fake_client.post.return_value = fake_http_response
        fake_context = AsyncMock()
        fake_context.__aenter__.return_value = fake_client
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("llm_council.openrouter")
        original_handlers = logger.handlers[:]
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            with patch("backend.openrouter.httpx.AsyncClient", return_value=fake_context):
                result = await openrouter.query_model(
                    "provider/model", [{"role": "user", "content": "private prompt"}]
                )
        finally:
            logger.handlers = original_handlers
        self.assertEqual(result["content"], "private answer")
        events = [getattr(record, "event_name", None) for record in records]
        self.assertEqual(events, ["openrouter.request.started", "openrouter.request.completed"])
        serialized_fields = repr([getattr(record, "event_fields", {}) for record in records])
        self.assertNotIn("private prompt", serialized_fields)
        self.assertNotIn("private answer", serialized_fields)

    async def test_http_failure_logs_status_and_returns_none(self):
        error = openrouter.httpx.HTTPStatusError(
            "payment required",
            request=openrouter.httpx.Request("POST", "https://openrouter.ai"),
            response=openrouter.httpx.Response(402),
        )
        fake_client = AsyncMock()
        fake_client.post.side_effect = error
        fake_context = AsyncMock()
        fake_context.__aenter__.return_value = fake_client
        with patch("backend.openrouter.httpx.AsyncClient", return_value=fake_context):
            result = await openrouter.query_model("provider/model", [{"role": "user", "content": "x"}])
        self.assertIsNone(result)
```

Add these tests, reusing the record-capture pattern above and restoring logger handlers in `finally` blocks:

```python
class CouncilLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_one_logs_start_completion_count_and_duration(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("llm_council.council")
        original_handlers = logger.handlers[:]
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            with patch(
                "backend.council.query_models_parallel",
                new=AsyncMock(return_value={"model/a": {"content": "answer"}}),
            ):
                result = await council.stage1_collect_responses("question")
        finally:
            logger.handlers = original_handlers
        self.assertEqual(len(result), 1)
        self.assertEqual(
            [record.event_name for record in records],
            ["council.stage.started", "council.stage.completed"],
        )
        self.assertEqual(records[-1].event_fields["response_count"], 1)
        self.assertGreaterEqual(records[-1].event_fields["duration_ms"], 0)

    async def test_all_models_failed_and_chairman_fallback_are_logged(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("llm_council.council")
        original_handlers = logger.handlers[:]
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            with patch("backend.council.stage1_collect_responses", new=AsyncMock(return_value=[])):
                result = await council.run_full_council("question")
            with patch("backend.council.query_model", new=AsyncMock(return_value=None)):
                fallback = await council.stage3_synthesize_final("question", [], [])
        finally:
            logger.handlers = original_handlers
        events = [record.event_name for record in records]
        self.assertIn("council.all_models_failed", events)
        self.assertIn("council.chairman_fallback", events)
        self.assertEqual(result[2]["model"], "error")
        self.assertIn("Unable", fallback["response"])


class PayloadLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_payloads_are_present_only_when_enabled_and_are_redacted(self):
        fake_http_response = Mock()
        fake_http_response.raise_for_status.return_value = None
        fake_http_response.json.return_value = {
            "choices": [{"message": {"content": "answer sk-or-v1-secretvalue000", "reasoning_details": None}}]
        }
        fake_client = AsyncMock()
        fake_client.post.return_value = fake_http_response
        fake_context = AsyncMock()
        fake_context.__aenter__.return_value = fake_client
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("llm_council.openrouter")
        original_handlers = logger.handlers[:]
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            with patch.dict("os.environ", {"LOG_LLM_PAYLOADS": "true"}), \
                 patch("backend.openrouter.httpx.AsyncClient", return_value=fake_context):
                await openrouter.query_model(
                    "provider/model",
                    [{"role": "user", "content": "prompt sk-or-v1-secretvalue111"}],
                )
        finally:
            logger.handlers = original_handlers
        serialized = repr([record.event_fields for record in records])
        self.assertIn("messages", serialized)
        self.assertIn("content", serialized)
        self.assertNotIn("secretvalue000", serialized)
        self.assertNotIn("secretvalue111", serialized)
```

- [ ] **Step 2: Run domain tests and verify they fail**

Run:

```bash
uv run python -m unittest tests.test_domain_logging -v
```

Expected: missing event records and continued `print` output from the current OpenRouter error path.

- [ ] **Step 3: Instrument OpenRouter and council stages**

In `backend/openrouter.py`, create a module logger, time every call with `time.perf_counter()`, split `httpx.TimeoutException`, `httpx.HTTPStatusError`, malformed-response errors, and unexpected exceptions into sanitized categories, and replace `print` with `log_event`. Log status codes but never response bodies on failures.

Use this event-field policy:

```python
fields = {
    "model": model,
    "message_count": len(messages),
}
if settings.log_llm_payloads:
    fields["messages"] = messages
log_event(logger, logging.INFO, "openrouter.request.started", "Model request started", **fields)
```

On completion, add `duration_ms`, response character count, and optionally sanitized response content. In `backend/council.py`, wrap each stage with start/end timing and record input/output counts. Keep public return values unchanged. Emit warning/error events for graceful-degradation branches.

- [ ] **Step 4: Run domain and regression tests**

Run:

```bash
uv run python -m unittest discover -v
```

Expected: all Python tests pass and the error-path test emits no raw `print` output.

- [ ] **Step 5: Commit domain instrumentation**

```bash
git add backend/openrouter.py backend/council.py tests/test_domain_logging.py
git commit -m "feature: instrument council model calls"
```

---

### Task 5: Development Supervisor, Manifest, Startup Wrapper, and Local Settings

**Files:**
- Create: `backend/dev_runner.py`
- Create: `tests/test_dev_runner.py`
- Modify: `start.sh:1-31`
- Modify: `.gitignore:15-21`
- Modify locally, do not stage: `.env`

**Interfaces:**
- Consumes: Task 1/2 run and logger APIs, repository root, `sys.executable`, `npm`, and OS signals.
- Produces: `ChildCommand(args: tuple[str, ...], cwd: Path, capture_output: bool)`, `build_child_commands(repo_root: Path) -> tuple[ChildCommand, ChildCommand]`, `atomic_write_manifest(path: Path, payload: Mapping[str, Any]) -> None`, `build_child_environment(settings: LoggingSettings, context: RunContext) -> dict[str, str]`, `stream_vite_output(stream: Iterable[str], logger: logging.Logger, terminal: TextIO) -> None`, `stop_children(children: Sequence[subprocess.Popen], grace_seconds: float) -> dict[int, int]`, and `run(settings: LoggingSettings | None = None, poll_interval: float = 0.1) -> int`.

- [ ] **Step 1: Write failing supervisor and manifest tests**

Create `tests/test_dev_runner.py`:

```python
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.dev_runner import atomic_write_manifest, run, stream_vite_output
from backend.logging_config import LoggingSettings


class ManifestTests(unittest.TestCase):
    def test_atomic_manifest_is_valid_json_and_contains_no_secret_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            atomic_write_manifest(
                path,
                {
                    "run_id": "2026-08-26T220712Z",
                    "settings": {"level": "INFO"},
                    "status": "starting",
                },
            )
            payload = json.loads(path.read_text())
            self.assertEqual(payload["status"], "starting")
            self.assertNotIn("OPENROUTER_API_KEY", path.read_text())


class ViteStreamingTests(unittest.TestCase):
    def test_mirrors_vite_line_and_writes_structured_event(self):
        terminal = io.StringIO()
        logger = Mock()
        stream_vite_output(iter(["VITE ready\n"]), logger, terminal)
        self.assertEqual(terminal.getvalue(), "VITE ready\n")
        logger.log.assert_called_once()


class SupervisorTests(unittest.TestCase):
    @patch("backend.dev_runner.subprocess.Popen")
    def test_child_failure_stops_sibling_and_returns_nonzero(self, popen):
        with tempfile.TemporaryDirectory() as directory:
            settings = LoggingSettings(log_dir=Path(directory))
            backend = Mock(pid=101)
            frontend = Mock(pid=102)
            backend.poll.side_effect = [None, 1]
            frontend.poll.return_value = None
            frontend.stdout = iter([])
            popen.side_effect = [backend, frontend]
            result = run(settings=settings, poll_interval=0)
            self.assertNotEqual(result, 0)
            frontend.terminate.assert_called()
            manifest_path = next(Path(directory).glob("runs/*/manifest.json"))
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["status"], "unclean")
            self.assertEqual(manifest["children"]["backend"]["exit_code"], 1)
            self.assertIn("startup_cleanup", manifest)
```

Extend imports with `replace`, `build_child_commands`, `build_child_environment`, `create_run_context`, and `stop_children`, then add:

```python
class ChildConfigurationTests(unittest.TestCase):
    def test_commands_use_active_python_and_expected_working_directories(self):
        root = Path("/project").resolve()
        backend, frontend = build_child_commands(root)
        self.assertEqual(backend.args[1:], ("-m", "backend.main"))
        self.assertEqual(backend.cwd, root)
        self.assertFalse(backend.capture_output)
        self.assertEqual(frontend.args, ("npm", "run", "dev"))
        self.assertEqual(frontend.cwd, root / "frontend")
        self.assertTrue(frontend.capture_output)

    def test_child_environment_contains_run_context_but_no_vite_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = LoggingSettings(log_dir=Path(directory))
            context = create_run_context(settings)
            with patch.dict("os.environ", {"OPENROUTER_API_KEY": "secret-marker"}):
                environment = build_child_environment(settings, context)
            self.assertEqual(environment["LLM_COUNCIL_RUN_ID"], context.run_id)
            self.assertEqual(environment["LLM_COUNCIL_RUN_DIR"], str(context.run_dir.resolve()))
            vite_values = {
                key: value for key, value in environment.items() if key.startswith("VITE_")
            }
            self.assertNotIn("secret-marker", repr(vite_values))


class ShutdownTests(unittest.TestCase):
    def test_graceful_stop_does_not_kill_child_that_exits(self):
        child = Mock(pid=101)
        child.poll.side_effect = [None, 0, 0]
        child.wait.return_value = 0
        result = stop_children([child], grace_seconds=0.1)
        child.terminate.assert_called_once()
        child.kill.assert_not_called()
        self.assertEqual(result[101], 0)

    def test_stop_kills_child_that_exceeds_grace_period(self):
        child = Mock(pid=102)
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("child", 0), -9]
        result = stop_children([child], grace_seconds=0)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(result[102], -9)
```

Import `subprocess` for the timeout assertion. The manifest secret assertion remains in `ManifestTests`.

- [ ] **Step 2: Run supervisor tests and verify they fail**

Run:

```bash
uv run python -m unittest tests.test_dev_runner -v
```

Expected: `ModuleNotFoundError: No module named 'backend.dev_runner'`.

- [ ] **Step 3: Implement the supervisor and manifest ownership**

Create `backend/dev_runner.py` with `run(settings: LoggingSettings | None = None, poll_interval: float = 0.1) -> int` and:

- Repository root derived from `Path(__file__).resolve().parents[1]`.
- `LoggingSettings.from_env()` and `create_run_context()` at startup.
- Cleanup before child launch and at shutdown.
- Atomic manifest writes using a sibling temporary file plus `os.replace`.
- Backend command `[sys.executable, "-m", "backend.main"]` with repository-root cwd and inherited terminal streams.
- Frontend command `["npm", "run", "dev"]` with `frontend/` cwd and combined piped stdout/stderr.
- Environment variables `LLM_COUNCIL_RUN_ID` and absolute `LLM_COUNCIL_RUN_DIR` for the backend.
- Explicit `VITE_LOG_BROWSER_LEVEL`, `VITE_LOG_BROWSER_BATCH_SIZE`, `VITE_LOG_BROWSER_FLUSH_MS`, `VITE_LOG_BROWSER_QUEUE_LIMIT`, and `VITE_LOG_EVENT_MAX_BYTES` for Vite; do not copy secrets into any `VITE_` variable.
- One daemon thread that mirrors Vite lines and logs `vite.output` records.
- Signal handlers that set a shutdown event; process stopping occurs in the main control flow rather than inside the signal handler.
- A five-second graceful termination window, then `kill()` only for children still alive.
- Final manifest status `clean` only when shutdown was requested and both children exit within the grace period with either zero or the expected `SIGINT`/`SIGTERM` exit status; otherwise `unclean` with exact child exit codes.

Use `configure_source_logger(name="llm_council.vite", source="vite", path=context.run_dir / "vite.jsonl", level=settings.vite_level, run_id=context.run_id, settings=settings, include_console=False)` for Vite, and write the original Vite line to `sys.stdout` so terminal behavior remains familiar.

Use this concrete control flow as the implementation spine:

```python
@dataclass(frozen=True)
class ChildCommand:
    args: tuple[str, ...]
    cwd: Path
    capture_output: bool


def build_child_commands(repo_root: Path) -> tuple[ChildCommand, ChildCommand]:
    return (
        ChildCommand((sys.executable, "-m", "backend.main"), repo_root, False),
        ChildCommand(("npm", "run", "dev"), repo_root / "frontend", True),
    )


def atomic_write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(redact(dict(payload)), handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def stream_vite_output(stream, logger: logging.Logger, terminal) -> None:
    for line in stream:
        terminal.write(line)
        terminal.flush()
        clean_line = ANSI_ESCAPE.sub("", line.rstrip("\n"))
        log_event(logger, logging.INFO, "vite.output", clean_line, stream="combined")


def stop_children(children, grace_seconds: float) -> dict[int, int]:
    results = {}
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            code = child.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            code = child.wait()
        results[child.pid] = code
    return results


def run(settings: LoggingSettings | None = None, poll_interval: float = 0.1) -> int:
    effective = settings or LoggingSettings.from_env()
    context = create_run_context(effective)
    startup_cleanup = cleanup_logs(effective, context.run_dir)
    manifest_path = context.run_dir / "manifest.json"
    child_env = build_child_environment(effective, context)
    backend_spec, frontend_spec = build_child_commands(REPO_ROOT)
    shutdown_requested = threading.Event()
    previous_handlers = {}

    def request_shutdown(signum, frame):
        del frame
        shutdown_requested.set()
        supervisor_state["signal"] = signum

    supervisor_state = {"signal": None, "unexpected_exit": False}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    manifest = {
        "run_id": context.run_id,
        "started_at": utc_timestamp(),
        "status": "starting",
        "settings": effective.to_safe_dict(),
        "startup_cleanup": [asdict(action) for action in startup_cleanup],
        "children": {},
    }
    atomic_write_manifest(manifest_path, manifest)
    backend = subprocess.Popen(backend_spec.args, cwd=backend_spec.cwd, env=child_env)
    frontend = subprocess.Popen(
        frontend_spec.args,
        cwd=frontend_spec.cwd,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    children = [backend, frontend]
    manifest["children"] = {
        "backend": {"pid": backend.pid, "command": list(backend_spec.args)},
        "frontend": {"pid": frontend.pid, "command": list(frontend_spec.args)},
    }
    manifest["status"] = "running"
    atomic_write_manifest(manifest_path, manifest)
    vite_logger = configure_source_logger(
        name="llm_council.vite",
        source="vite",
        path=context.run_dir / "vite.jsonl",
        level=effective.vite_level,
        run_id=context.run_id,
        settings=effective,
        include_console=False,
    )
    vite_thread = threading.Thread(
        target=stream_vite_output,
        args=(frontend.stdout, vite_logger, sys.stdout),
        daemon=True,
    )
    vite_thread.start()

    try:
        while not shutdown_requested.wait(poll_interval):
            if backend.poll() is not None or frontend.poll() is not None:
                supervisor_state["unexpected_exit"] = True
                break
    finally:
        exit_codes = stop_children(children, grace_seconds=5.0)
        vite_thread.join(timeout=5.0)
        shutdown_cleanup = cleanup_logs(effective, context.run_dir)
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)

    expected_codes = {
        0,
        -signal.SIGINT,
        -signal.SIGTERM,
        128 + signal.SIGINT,
        128 + signal.SIGTERM,
    }
    clean = (
        shutdown_requested.is_set()
        and not supervisor_state["unexpected_exit"]
        and all(code in expected_codes for code in exit_codes.values())
    )
    manifest["status"] = "clean" if clean else "unclean"
    manifest["ended_at"] = utc_timestamp()
    manifest["shutdown_signal"] = supervisor_state["signal"]
    manifest["shutdown_cleanup"] = [asdict(action) for action in shutdown_cleanup]
    manifest["children"]["backend"]["exit_code"] = exit_codes[backend.pid]
    manifest["children"]["frontend"]["exit_code"] = exit_codes[frontend.pid]
    atomic_write_manifest(manifest_path, manifest)
    return 0 if clean else next((code for code in exit_codes.values() if code), 1)
```

Define `utc_timestamp()` in this module as UTC RFC 3339 with milliseconds. Define `build_child_environment()` by copying `os.environ`, adding the two run variables and only the five approved `VITE_LOG_*` values. Define `ANSI_ESCAPE` as a compiled CSI escape-sequence regex. If either `Popen` call raises, stop any child already created, record `status="unclean"` and a sanitized startup error in the manifest, then return nonzero.

- [ ] **Step 4: Replace shell job control, ignore generated logs, and add local `.env` details**

Replace `start.sh` with:

```bash
#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
exec uv run python -m backend.dev_runner
```

Append `logs/` to `.gitignore` under a `# Runtime logs` heading.

Prepend this approved block to the ignored `.env` without reading, printing, replacing, or staging the existing `OPENROUTER_API_KEY` line:

```dotenv
# General logging
LOG_LEVEL=INFO
LOG_DIR=logs

# Per-source overrides
LOG_BACKEND_LEVEL=INFO
LOG_UVICORN_LEVEL=INFO
LOG_VITE_LEVEL=INFO
LOG_BROWSER_LEVEL=WARNING

# Rotation and retention
LOG_MAX_BYTES=10485760
LOG_RETENTION_DAYS=14
LOG_TOTAL_MAX_BYTES=524288000

# Sensitive LLM content; leave disabled for normal development
LOG_LLM_PAYLOADS=false

# Browser batching and safety limits
LOG_BROWSER_BATCH_SIZE=20
LOG_BROWSER_FLUSH_MS=2000
LOG_BROWSER_QUEUE_LIMIT=200
LOG_EVENT_MAX_BYTES=65536
```

Use `apply_patch` to prepend the block without including the existing key line as patch context. Verify only variable names with `rg '^LOG_[A-Z_]+=' .env | cut -d= -f1`; never print values.

- [ ] **Step 5: Run supervisor, core, and syntax tests**

Run:

```bash
uv run python -m unittest tests.test_dev_runner tests.test_logging_config tests.test_log_retention -v
uv run python -m compileall -q backend tests
bash -n start.sh
git check-ignore logs/example.jsonl .env
```

Expected: tests and syntax checks pass; both `logs/example.jsonl` and `.env` are reported as ignored.

- [ ] **Step 6: Commit tracked supervisor and startup changes**

Confirm `.env` is not staged:

```bash
git status -sb
git add backend/dev_runner.py tests/test_dev_runner.py start.sh .gitignore
git diff --cached --name-only
git commit -m "feature: supervise local services with durable logs"
```

Expected staged names: only `backend/dev_runner.py`, `tests/test_dev_runner.py`, `start.sh`, and `.gitignore`.

---

### Task 6: Browser Logger, Frontend Integration, and Clean Frontend Checks

**Files:**
- Create: `frontend/src/logger.js`
- Create: `frontend/src/logger.test.js`
- Modify: `frontend/src/api.js:1-115`
- Modify: `frontend/src/main.jsx:1-10`
- Modify: `frontend/src/App.jsx:1-41`
- Modify: `frontend/src/components/Sidebar.jsx:1`
- Modify: `frontend/package.json:6-11`

**Interfaces:**
- Consumes: browser events, console methods, `API_BASE`, supervisor-provided `VITE_LOG_*` settings, and `POST /api/logs/browser`.
- Produces: `createBrowserLogger(options) -> {start, stop, log, flush}`, automatic error/rejection/console capture, and `npm test`.

- [ ] **Step 1: Write failing Node-native browser logger tests**

Create `frontend/src/logger.test.js` with dependency-injected fakes:

```javascript
import assert from 'node:assert/strict';
import test from 'node:test';

import { createBrowserLogger } from './logger.js';

function createFakeWindow() {
  const listeners = new Map();
  return {
    location: { href: 'http://localhost:5173/' },
    addEventListener(name, callback) {
      listeners.set(name, callback);
    },
    removeEventListener(name) {
      listeners.delete(name);
    },
    dispatch(name, event) {
      listeners.get(name)?.(event);
    },
  };
}

test('preserves console.error and batches a sanitized event', async () => {
  const sent = [];
  const originalCalls = [];
  const consoleObject = {
    warn() {},
    error(...args) {
      originalCalls.push(args);
    },
    log() {},
    info() {},
    debug() {},
  };
  const logger = createBrowserLogger({
    endpoint: 'http://localhost:8001/api/logs/browser',
    level: 'WARNING',
    batchSize: 1,
    flushMs: 2000,
    queueLimit: 2,
    eventMaxBytes: 65536,
    windowObject: createFakeWindow(),
    consoleObject,
    transport: async (endpoint, body) => sent.push({ endpoint, body }),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.start();
  consoleObject.error('failure', { authorization: 'Bearer secret-value' });
  await logger.flush();
  logger.stop();
  assert.equal(originalCalls.length, 1);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].body.events[0].event, 'browser.console.error');
  assert.doesNotMatch(JSON.stringify(sent), /secret-value/);
});

test('queue overflow drops the oldest event', async () => {
  const sent = [];
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'DEBUG',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 2,
    eventMaxBytes: 65536,
    windowObject: createFakeWindow(),
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.log('INFO', 'first', 'first');
  logger.log('INFO', 'second', 'second');
  logger.log('INFO', 'third', 'third');
  await logger.flush();
  assert.deepEqual(sent[0].events.map((event) => event.event), ['second', 'third']);
});
```

Add these tests using the same fake-window and injected dependency pattern:

```javascript
test('captures window errors and unhandled rejections', async () => {
  const sent = [];
  const windowObject = createFakeWindow();
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 65536,
    windowObject,
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.start();
  windowObject.dispatch('error', { message: 'boom', error: new Error('boom') });
  windowObject.dispatch('unhandledrejection', { reason: new Error('rejected') });
  await logger.flush();
  logger.stop();
  assert.deepEqual(
    sent[0].events.map((event) => event.event),
    ['browser.unhandled_error', 'browser.unhandled_rejection'],
  );
});

test('timer and pagehide flush pending events', async () => {
  const sent = [];
  const beacons = [];
  const windowObject = createFakeWindow();
  let scheduledCallback;
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'DEBUG',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 65536,
    windowObject,
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    beacon: (endpoint, body) => beacons.push({ endpoint, body }),
    setTimeoutFn: (callback) => {
      scheduledCallback = callback;
      return 1;
    },
    clearTimeoutFn: () => {},
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.start();
  logger.log('INFO', 'timer.event', 'timer');
  await scheduledCallback();
  assert.equal(sent.length, 1);
  logger.log('INFO', 'unload.event', 'unload');
  windowObject.dispatch('pagehide', {});
  assert.equal(beacons.length, 1);
  logger.stop();
});

test('sanitizes cyclic and oversized details and filters levels', async () => {
  const sent = [];
  const cyclic = { authorization: 'Bearer secret-value' };
  cyclic.self = cyclic;
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 256,
    windowObject: createFakeWindow(),
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.log('INFO', 'filtered.event', 'not sent');
  logger.log('ERROR', 'error.event', 'á'.repeat(500), cyclic);
  await logger.flush();
  const serialized = JSON.stringify(sent);
  assert.doesNotMatch(serialized, /secret-value/);
  assert.doesNotMatch(serialized, /filtered\.event/);
  assert.match(serialized, /truncated|error\.event/);
});

test('stop restores console and transport rejection is contained', async () => {
  const originalErrors = [];
  const originalError = (...args) => originalErrors.push(args);
  const consoleObject = {
    warn() {},
    error: originalError,
    log() {},
    info() {},
    debug() {},
  };
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 1,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 65536,
    windowObject: createFakeWindow(),
    consoleObject,
    transport: async () => {
      throw new Error('transport unavailable');
    },
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.start();
  consoleObject.error('application error');
  await assert.doesNotReject(logger.flush());
  logger.stop();
  assert.equal(consoleObject.error, originalError);
  assert.equal(originalErrors.length, 1);
});
```

- [ ] **Step 2: Add the test script and verify tests fail**

Add this script to `frontend/package.json`:

```json
"test": "node --test src/logger.test.js"
```

Run:

```bash
npm --prefix frontend test
```

Expected: `ERR_MODULE_NOT_FOUND` for `src/logger.js`.

- [ ] **Step 3: Implement the bounded browser logger**

Create `frontend/src/logger.js` with the exact factory interface exercised by the tests. Implement:

- Numeric severity ordering for `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`.
- A recursive sanitizer with a `WeakSet` for cycles, sensitive-key replacement, token-pattern replacement, and UTF-8 size truncation.
- A queue that drops from the front before adding beyond `queueLimit`.
- Count-triggered and timer-triggered flushes.
- One in-flight flush at a time; failed batches are dropped after bounded best effort and never call wrapped console methods.
- `fetch` transport using JSON and no credentials; use `navigator.sendBeacon` only for the final unload flush when available.
- Original console calls before queueing captured events.
- Stable listener function references so `stop()` fully removes them and restores console methods.

Use this concrete queue/lifecycle implementation, with `sanitizeBrowserValue()` implementing the tested recursive redaction, cycle handling, and UTF-8 truncation before `buildEvent()` returns:

```javascript
const SENSITIVE_KEY = /authorization|proxy_authorization|cookie|set_cookie|password|secret|token|api_key/i;
const TOKEN_VALUE = /(?:bearer\s+|sk-(?:or-)?[a-z0-9-]*)([a-z0-9_-]{12,})/gi;

function truncateBrowserText(value, maxBytes) {
  const encoded = new TextEncoder().encode(value);
  if (encoded.byteLength <= maxBytes) return value;
  return `${new TextDecoder().decode(encoded.slice(0, maxBytes))}[truncated:${encoded.byteLength}]`;
}

export function sanitizeBrowserValue(value, { eventMaxBytes, seen }) {
  if (typeof value === 'string') {
    return truncateBrowserText(
      value.replace(TOKEN_VALUE, (match) => `${match.slice(0, 8)}[REDACTED]`),
      eventMaxBytes,
    );
  }
  if (value === null || ['boolean', 'number'].includes(typeof value)) return value;
  if (value instanceof Error) {
    return sanitizeBrowserValue(
      { name: value.name, message: value.message, stack: value.stack },
      { eventMaxBytes, seen },
    );
  }
  if (typeof value !== 'object') return truncateBrowserText(String(value), eventMaxBytes);
  if (seen.has(value)) return '[Circular]';
  seen.add(value);
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeBrowserValue(item, { eventMaxBytes, seen }));
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      SENSITIVE_KEY.test(key)
        ? '[REDACTED]'
        : sanitizeBrowserValue(item, { eventMaxBytes, seen }),
    ]),
  );
}

export function createBrowserLogger(options) {
  const {
    endpoint,
    level,
    batchSize,
    flushMs,
    queueLimit,
    eventMaxBytes,
    windowObject,
    consoleObject,
    transport,
    beacon,
    setTimeoutFn = setTimeout,
    clearTimeoutFn = clearTimeout,
    now = () => new Date().toISOString(),
    sessionId = crypto.randomUUID(),
  } = options;
  const state = {
    queue: [],
    started: false,
    flushing: false,
    timerId: null,
  };
  const originals = new Map();
  const severity = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

  function enabled(eventLevel) {
    return severity[eventLevel] >= severity[level];
  }

  function buildEvent(eventLevel, event, message, details = {}) {
    const safe = sanitizeBrowserValue(
      { message: String(message), details },
      { eventMaxBytes, seen: new WeakSet() },
    );
    return {
      client_timestamp: now(),
      level: eventLevel,
      event,
      message: safe.message,
      browser_session_id: sessionId,
      page: windowObject.location.href,
      details: safe.details,
    };
  }

  function enqueue(eventPayload) {
    while (state.queue.length >= queueLimit) state.queue.shift();
    state.queue.push(eventPayload);
    if (state.queue.length >= batchSize) void flush();
  }

  function log(eventLevel, event, message, details = {}) {
    if (!enabled(eventLevel)) return;
    enqueue(buildEvent(eventLevel, event, message, details));
  }

  async function flush() {
    if (state.flushing || state.queue.length === 0) return;
    state.flushing = true;
    const events = state.queue.splice(0, batchSize);
    try {
      await transport(endpoint, { events });
    } catch {
      return;
    } finally {
      state.flushing = false;
    }
    if (state.queue.length > 0) await flush();
  }

  function scheduleFlush() {
    if (!state.started) return;
    state.timerId = setTimeoutFn(async () => {
      await flush();
      scheduleFlush();
    }, flushMs);
  }

  function onError(event) {
    log('ERROR', 'browser.unhandled_error', event.message, {
      stack: event.error?.stack,
    });
  }

  function onUnhandledRejection(event) {
    const reason = event.reason instanceof Error
      ? { message: event.reason.message, stack: event.reason.stack }
      : { reason: event.reason };
    log('ERROR', 'browser.unhandled_rejection', reason.message ?? 'Unhandled rejection', reason);
  }

  function onPageHide() {
    if (state.queue.length === 0 || !beacon) return;
    const events = state.queue.splice(0, batchSize);
    try {
      beacon(endpoint, JSON.stringify({ events }));
    } catch {
      return;
    }
  }

  function wrapConsole(method, eventLevel) {
    const original = consoleObject[method].bind(consoleObject);
    originals.set(method, consoleObject[method]);
    consoleObject[method] = (...args) => {
      original(...args);
      log(eventLevel, `browser.console.${method}`, args.map(String).join(' '), { args });
    };
  }

  function start() {
    if (state.started) return;
    state.started = true;
    wrapConsole('warn', 'WARNING');
    wrapConsole('error', 'ERROR');
    if (level === 'DEBUG') {
      wrapConsole('debug', 'DEBUG');
      wrapConsole('log', 'DEBUG');
      wrapConsole('info', 'INFO');
    }
    windowObject.addEventListener('error', onError);
    windowObject.addEventListener('unhandledrejection', onUnhandledRejection);
    windowObject.addEventListener('pagehide', onPageHide);
    scheduleFlush();
  }

  function stop() {
    if (!state.started) return;
    state.started = false;
    if (state.timerId !== null) clearTimeoutFn(state.timerId);
    windowObject.removeEventListener('error', onError);
    windowObject.removeEventListener('unhandledrejection', onUnhandledRejection);
    windowObject.removeEventListener('pagehide', onPageHide);
    for (const [method, original] of originals) consoleObject[method] = original;
    originals.clear();
  }

  return {
    start,
    stop,
    log,
    flush,
  };
}
```

Export `API_BASE` from `frontend/src/api.js`. Add `installBrowserLogging()` that reads only the approved `VITE_LOG_*` variables with validated fallbacks and targets `${API_BASE}/api/logs/browser`.

- [ ] **Step 4: Install logging once and fix the known lint baseline**

In `frontend/src/main.jsx`, install before React rendering and dispose during hot reload:

```javascript
import { installBrowserLogging } from './logger.js'

const browserLogger = installBrowserLogging()
browserLogger.start()

if (import.meta.hot) {
  import.meta.hot.dispose(() => browserLogger.stop())
}
```

In `App.jsx`, import `useCallback`, declare `loadConversations` and `loadConversation` before their effects, wrap them in `useCallback`, and include them in effect dependency arrays. Keep behavior unchanged. In `Sidebar.jsx`, remove the unused React import entirely.

- [ ] **Step 5: Run frontend tests, lint, and build**

Run:

```bash
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: all Node tests pass, ESLint reports zero errors, and Vite completes the production build.

- [ ] **Step 6: Commit browser logging and frontend cleanup**

```bash
git add frontend/src/logger.js frontend/src/logger.test.js frontend/src/api.js frontend/src/main.jsx frontend/src/App.jsx frontend/src/components/Sidebar.jsx frontend/package.json
git commit -m "feature: capture browser diagnostics"
```

---

### Task 7: Real-Process Smoke Coverage, Documentation, and Full Verification

**Files:**
- Create: `tests/test_logging_smoke.py`
- Modify: `README.md:40-83`

**Interfaces:**
- Consumes: the completed supervisor, browser endpoint, temporary `LOG_DIR`, reduced rotation settings, and local ports 8001/5173.
- Produces: opt-in `RUN_LOGGING_SMOKE=1` validation and user-facing logging documentation.

- [ ] **Step 1: Write the opt-in real-process smoke test**

Create `tests/test_logging_smoke.py` with `@unittest.skipUnless(os.getenv("RUN_LOGGING_SMOKE") == "1", "set RUN_LOGGING_SMOKE=1 to run")`. The test must:

1. Create a temporary log directory.
2. Launch `./start.sh` with `start_new_session=True`, `LOG_DIR` set to the temporary path, `LOG_MAX_BYTES=2048`, `LOG_RETENTION_DAYS=14`, `LOG_TOTAL_MAX_BYTES=65536`, `OPENROUTER_API_KEY=smoke-secret-marker`, and no council request.
3. Poll `http://localhost:8001/` and `http://localhost:5173/` with a 20-second deadline.
4. POST one browser warning with `Origin: http://localhost:5173`.
5. Send `SIGINT` to the launched process group and wait up to 10 seconds.
6. Resolve `logs/latest`, parse every non-empty line in all four JSONL files, and parse `manifest.json`.
7. Assert the browser event exists, Uvicorn recorded the health request, Vite recorded startup, manifest status is `clean`, and no file contains `smoke-secret-marker`.
8. In `finally`, terminate and kill the process group only if it remains alive.

Use standard-library `subprocess`, `signal`, `urllib.request`, `tempfile`, and `json`; do not add dependencies.

Use this concrete test body:

```python
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


def wait_for_url(url: str, deadline_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as error:
            last_error = error
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {url}: {last_error}")


@unittest.skipUnless(
    os.getenv("RUN_LOGGING_SMOKE") == "1",
    "set RUN_LOGGING_SMOKE=1 to run",
)
class LoggingSmokeTests(unittest.TestCase):
    def test_start_script_creates_redacted_logs_and_shuts_down_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_dir = root / "logs"
            terminal_path = root / "terminal.log"
            environment = os.environ.copy()
            environment.update(
                {
                    "LOG_DIR": str(log_dir),
                    "LOG_MAX_BYTES": "2048",
                    "LOG_RETENTION_DAYS": "14",
                    "LOG_TOTAL_MAX_BYTES": "65536",
                    "OPENROUTER_API_KEY": "smoke-secret-marker",
                }
            )
            process = None
            with terminal_path.open("w", encoding="utf-8") as terminal:
                try:
                    process = subprocess.Popen(
                        ["./start.sh"],
                        cwd=Path(__file__).resolve().parents[1],
                        env=environment,
                        stdout=terminal,
                        stderr=subprocess.STDOUT,
                        text=True,
                        start_new_session=True,
                    )
                    wait_for_url("http://localhost:8001/")
                    wait_for_url("http://localhost:5173/")
                    body = json.dumps(
                        {
                            "events": [
                                {
                                    "client_timestamp": "2026-08-26T22:07:12.438Z",
                                    "level": "WARNING",
                                    "event": "browser.smoke_warning",
                                    "message": "smoke browser warning",
                                    "browser_session_id": "smoke-session",
                                    "page": "http://localhost:5173/",
                                }
                            ]
                        }
                    ).encode("utf-8")
                    request = urllib.request.Request(
                        "http://localhost:8001/api/logs/browser",
                        data=body,
                        headers={
                            "Content-Type": "application/json",
                            "Origin": "http://localhost:5173",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, 202)
                    os.killpg(process.pid, signal.SIGINT)
                    self.assertEqual(process.wait(timeout=10), 0)
                finally:
                    if process is not None and process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)

            run_dir = (log_dir / "latest").resolve()
            names = ["backend.jsonl", "uvicorn.jsonl", "vite.jsonl", "browser.jsonl"]
            events = {}
            for name in names:
                path = run_dir / name
                self.assertTrue(path.exists(), name)
                events[name] = [json.loads(line) for line in path.read_text().splitlines() if line]
            manifest = json.loads((run_dir / "manifest.json").read_text())
            all_text = "\n".join(path.read_text() for path in run_dir.iterdir() if path.is_file())
            terminal_text = terminal_path.read_text()
            self.assertEqual(manifest["status"], "clean")
            self.assertTrue(any(event["event"] == "browser.smoke_warning" for event in events["browser.jsonl"]))
            self.assertTrue(any(event.get("path") == "/" for event in events["uvicorn.jsonl"]))
            self.assertTrue(any(event["event"] == "vite.output" for event in events["vite.jsonl"]))
            self.assertTrue(any(event["event"] == "backend.started" for event in events["backend.jsonl"]))
            self.assertNotIn("smoke-secret-marker", all_text)
            self.assertIn("smoke browser warning", terminal_text)
```

- [ ] **Step 2: Run the real-process smoke test**

Stop any currently owned `start.sh` session so ports 8001 and 5173 are free. Run:

```bash
RUN_LOGGING_SMOKE=1 uv run python -m unittest tests.test_logging_smoke -v
```

Expected: the smoke test passes, makes no OpenRouter request, finds all four sources, and confirms a clean shutdown. A failure means the responsible earlier task is incomplete; diagnose it without weakening source, redaction, or shutdown assertions.

- [ ] **Step 3: Document operation and agent access**

Add a `## Logging` section to `README.md` containing:

- `./start.sh` as the start command.
- `tail -f logs/latest/backend.jsonl` and equivalent commands for Uvicorn, Vite, and browser logs.
- The run-directory and `logs/latest` layout.
- Default 10 MiB rotation, 14-day retention, and 500 MiB cap.
- The metadata-only default and explicit warning that `LOG_LLM_PAYLOADS=true` can record user/model content.
- The complete logging variable table with defaults.
- The fallback behavior when the log directory is unwritable.
- The opt-in smoke command and statement that it consumes no OpenRouter credits.

- [ ] **Step 4: Run the complete verification matrix**

Run from the repository root:

```bash
uv sync --locked
uv run python -m unittest discover -v
uv run python -m compileall -q backend tests
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
RUN_LOGGING_SMOKE=1 uv run python -m unittest tests.test_logging_smoke -v
git diff --check
git status -sb
```

Expected:

- All Python and Node tests pass.
- Python compilation succeeds.
- ESLint reports zero errors.
- Vite production build succeeds.
- Smoke test passes with four valid JSONL sources, clean shutdown, and no marker secret.
- `git diff --check` emits no output.
- `git status` shows only intended tracked changes; `.env`, `logs/`, `.venv`, `frontend/node_modules`, and `frontend/dist` remain ignored.

- [ ] **Step 5: Commit smoke coverage and documentation**

```bash
git add tests/test_logging_smoke.py README.md
git commit -m "test: verify agent-friendly logging workflow"
```

- [ ] **Step 6: Review the final branch diff and rollback surface**

Run:

```bash
git log --oneline --decorate -8
git diff b2d4657..HEAD --stat
git diff b2d4657..HEAD -- . ':!docs/superpowers/plans/2026-08-26-agent-friendly-logging.md'
```

Confirm the diff contains no key values, no generated logs, no package dependency additions, and no council behavior changes. Rollback consists of reverting the six feature/test commits; runtime `logs/` can be removed only after both services stop.
