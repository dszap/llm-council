"""Repository-aware development supervisor for backend and frontend services."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from dotenv import load_dotenv

from .logging_config import (
    LoggingSettings,
    RunContext,
    cleanup_logs,
    configure_source_logger,
    create_run_context,
    log_event,
    redact,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
APPROVED_VITE_SETTINGS = {
    "VITE_LOG_BROWSER_LEVEL": "browser_level",
    "VITE_LOG_BROWSER_BATCH_SIZE": "browser_batch_size",
    "VITE_LOG_BROWSER_FLUSH_MS": "browser_flush_ms",
    "VITE_LOG_BROWSER_QUEUE_LIMIT": "browser_queue_limit",
    "VITE_LOG_EVENT_MAX_BYTES": "event_max_bytes",
}


@dataclass(frozen=True)
class ChildCommand:
    """Command and working-directory details for a supervised child process."""

    args: tuple[str, ...]
    cwd: Path
    capture_output: bool


def build_child_commands(repo_root: Path) -> tuple[ChildCommand, ChildCommand]:
    """Build backend and frontend commands rooted at the active repository."""
    return (
        ChildCommand((sys.executable, "-m", "backend.main"), repo_root, False),
        ChildCommand(("npm", "run", "dev"), repo_root / "frontend", True),
    )


def atomic_write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace a manifest with a redacted JSON representation."""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(redact(dict(payload)), handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def build_child_environment(
    settings: LoggingSettings, context: RunContext
) -> dict[str, str]:
    """Copy the host environment while exposing only approved Vite settings."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("VITE_")
    }
    environment["LLM_COUNCIL_RUN_ID"] = context.run_id
    environment["LLM_COUNCIL_RUN_DIR"] = str(context.run_dir.resolve())
    for environment_name, setting_name in APPROVED_VITE_SETTINGS.items():
        environment[environment_name] = str(getattr(settings, setting_name))
    return environment


def stream_vite_output(
    stream: Iterable[str], logger: logging.Logger, terminal: TextIO
) -> None:
    """Mirror Vite output to the terminal and persist structured copies."""
    for line in stream:
        terminal.write(line)
        terminal.flush()
        clean_line = ANSI_ESCAPE.sub("", line.rstrip("\n"))
        log_event(
            logger,
            logging.INFO,
            "vite.output",
            clean_line,
            stream="combined",
        )


def stop_children(
    children: Sequence[subprocess.Popen],
    grace_seconds: float,
    shutdown_signal: int | signal.Signals = signal.SIGTERM,
) -> dict[int, int]:
    """Signal children together, then kill only those exceeding the grace period."""
    results: dict[int, int] = {}
    running: list[subprocess.Popen] = []
    for child in children:
        code = child.poll()
        if code is None:
            if shutdown_signal == signal.SIGTERM:
                child.terminate()
            else:
                child.send_signal(shutdown_signal)
            running.append(child)
        else:
            results[child.pid] = code

    deadline = time.monotonic() + grace_seconds
    for child in running:
        timeout = max(0.0, deadline - time.monotonic())
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            code = child.wait()
        results[child.pid] = code
    return results


def utc_timestamp() -> str:
    """Return a UTC RFC 3339 timestamp with millisecond precision."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _cleanup_payload(actions: Sequence[Any]) -> list[dict[str, Any]]:
    return [asdict(action) for action in actions]


def _close_logger(logger: logging.Logger) -> None:
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()


def run(
    settings: LoggingSettings | None = None, poll_interval: float = 0.1
) -> int:
    """Supervise local backend and frontend processes for one durable log run."""
    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)
    effective = settings or LoggingSettings.from_env()
    context = create_run_context(effective)
    startup_cleanup = cleanup_logs(effective, context.run_dir)
    manifest_path = context.run_dir / "manifest.json"
    child_environment = build_child_environment(effective, context)
    backend_spec, frontend_spec = build_child_commands(REPO_ROOT)
    shutdown_requested = threading.Event()
    supervisor_state = {"signal": None, "unexpected_exit": False}
    previous_handlers: dict[signal.Signals, Any] = {}
    children: list[subprocess.Popen] = []
    known_exit_codes: dict[int, int] = {}
    vite_thread: threading.Thread | None = None
    vite_logger: logging.Logger | None = None
    backend: subprocess.Popen | None = None
    frontend: subprocess.Popen | None = None
    startup_error: OSError | None = None

    def request_shutdown(signum: int, frame: Any) -> None:
        del frame
        supervisor_state["signal"] = signum
        shutdown_requested.set()

    manifest: dict[str, Any] = {
        "run_id": context.run_id,
        "started_at": utc_timestamp(),
        "status": "starting",
        "settings": effective.to_safe_dict(),
        "startup_cleanup": _cleanup_payload(startup_cleanup),
        "children": {},
    }
    atomic_write_manifest(manifest_path, manifest)

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_shutdown)

    try:
        try:
            backend = subprocess.Popen(
                backend_spec.args,
                cwd=backend_spec.cwd,
                env=child_environment,
            )
            children.append(backend)
            frontend = subprocess.Popen(
                frontend_spec.args,
                cwd=frontend_spec.cwd,
                env=child_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            children.append(frontend)
        except OSError as error:
            startup_error = error

        if startup_error is None:
            assert backend is not None and frontend is not None
            manifest["children"] = {
                "backend": {
                    "pid": backend.pid,
                    "command": list(backend_spec.args),
                },
                "frontend": {
                    "pid": frontend.pid,
                    "command": list(frontend_spec.args),
                },
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

            while not shutdown_requested.wait(poll_interval):
                backend_code = backend.poll()
                frontend_code = frontend.poll()
                if backend_code is not None or frontend_code is not None:
                    supervisor_state["unexpected_exit"] = True
                    if backend_code is not None:
                        known_exit_codes[backend.pid] = backend_code
                    if frontend_code is not None:
                        known_exit_codes[frontend.pid] = frontend_code
                    break
    finally:
        children_to_stop = [
            child for child in children if child.pid not in known_exit_codes
        ]
        exit_codes = {
            **known_exit_codes,
            **stop_children(
                children_to_stop,
                grace_seconds=5.0,
                shutdown_signal=supervisor_state["signal"] or signal.SIGTERM,
            ),
        }
        if vite_thread is not None:
            vite_thread.join(timeout=5.0)
        shutdown_cleanup = cleanup_logs(effective, context.run_dir)
        if vite_logger is not None:
            _close_logger(vite_logger)
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)

    if startup_error is not None:
        manifest["status"] = "unclean"
        manifest["ended_at"] = utc_timestamp()
        manifest["startup_error"] = (
            f"{type(startup_error).__name__}: child process failed to start"
        )
        manifest["shutdown_cleanup"] = _cleanup_payload(shutdown_cleanup)
        if backend is not None:
            manifest["children"] = {
                "backend": {
                    "pid": backend.pid,
                    "command": list(backend_spec.args),
                    "exit_code": exit_codes[backend.pid],
                }
            }
        atomic_write_manifest(manifest_path, manifest)
        return 1

    assert backend is not None and frontend is not None

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
        and len(exit_codes) == 2
        and all(code in expected_codes for code in exit_codes.values())
    )
    manifest["status"] = "clean" if clean else "unclean"
    manifest["ended_at"] = utc_timestamp()
    manifest["shutdown_signal"] = supervisor_state["signal"]
    manifest["shutdown_cleanup"] = _cleanup_payload(shutdown_cleanup)
    manifest["children"]["backend"]["exit_code"] = exit_codes[backend.pid]
    manifest["children"]["frontend"]["exit_code"] = exit_codes[frontend.pid]
    atomic_write_manifest(manifest_path, manifest)

    if clean:
        return 0
    return next((code for code in exit_codes.values() if code), 1)


if __name__ == "__main__":
    raise SystemExit(run())
