import json
import logging
import multiprocessing
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
    retention_lock,
)


def _cleanup_worker(settings, active, started, finished):
    started.set()
    cleanup_logs(settings, active)
    finished.set()


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
