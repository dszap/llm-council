import json
import logging
import multiprocessing
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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


def _hold_lock_worker(log_dir, acquired, release):
    with retention_lock(log_dir):
        acquired.set()
        release.wait(5)


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

    def test_collision_suffixes_directory_and_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(LoggingSettings(), log_dir=Path(directory))
            now = datetime(2026, 8, 26, 22, 7, 12, tzinfo=timezone.utc)
            first = create_run_context(settings, now=now)
            second = create_run_context(settings, now=now)
            self.assertEqual(first.run_id, "2026-08-26T220712Z")
            self.assertEqual(second.run_id, "2026-08-26T220712Z-1")
            self.assertEqual(first.run_dir.name, "2026-08-26T220712Z")
            self.assertEqual(second.run_dir.name, "2026-08-26T220712Z-1")

    def test_concurrent_creation_allocates_unique_run_ids_directories_and_temp_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(LoggingSettings(), log_dir=root)
            now = datetime(2026, 8, 26, 22, 7, 12, tzinfo=timezone.utc)
            base_run_id = "2026-08-26T220712Z"
            directory_barrier = threading.Barrier(2)
            link_barrier = threading.Barrier(2)
            original_mkdir = Path.mkdir
            original_symlink_to = Path.symlink_to

            def synchronized_mkdir(path, *args, **kwargs):
                if path.parent.name == "runs" and path.name == base_run_id:
                    directory_barrier.wait(timeout=2)
                return original_mkdir(path, *args, **kwargs)

            def synchronized_symlink_to(path, *args, **kwargs):
                if path.parent == root and path.name.startswith(".latest."):
                    link_barrier.wait(timeout=2)
                return original_symlink_to(path, *args, **kwargs)

            with patch.object(Path, "mkdir", synchronized_mkdir), patch.object(
                Path, "symlink_to", synchronized_symlink_to
            ), ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(create_run_context, settings, now)
                    for _ in range(2)
                ]
                errors = [future.exception(timeout=3) for future in futures]

            self.assertEqual(errors, [None, None])
            contexts = [future.result() for future in futures]
            self.assertEqual(
                {context.run_id for context in contexts},
                {base_run_id, f"{base_run_id}-1"},
            )
            self.assertEqual(len({context.run_dir for context in contexts}), 2)
            self.assertEqual({context.run_dir.name for context in contexts}, {context.run_id for context in contexts})
            self.assertTrue(all(context.run_dir.is_dir() for context in contexts))
            self.assertIn(
                (root / "latest").resolve(),
                {context.run_dir.resolve() for context in contexts},
            )
            self.assertEqual(list(root.glob(".latest.*")), [])


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

    def test_rollover_waits_for_retention_lock_before_shifting_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(LoggingSettings(), log_dir=root, max_bytes=128)
            context = create_run_context(settings)
            base = context.run_dir / "backend.jsonl"
            base.write_text("{}\n")
            logger = configure_source_logger(
                name="test.rotation.lock",
                source="backend",
                path=base,
                level="INFO",
                run_id=context.run_id,
                settings=settings,
                include_console=False,
            )
            handler = logger.handlers[0]
            attempted = threading.Event()
            shifted = threading.Event()
            errors = []
            original_rollover = handler.doRollover

            def observed_rollover():
                attempted.set()
                original_rollover()
                shifted.set()

            def emit_event():
                try:
                    log_event(logger, logging.INFO, "rotation.locked", "x" * 80)
                except Exception as error:
                    errors.append(error)

            handler.doRollover = observed_rollover
            process_context = multiprocessing.get_context("spawn")
            acquired = process_context.Event()
            release = process_context.Event()
            holder = process_context.Process(
                target=_hold_lock_worker,
                args=(root, acquired, release),
            )
            holder.start()
            self.assertTrue(acquired.wait(1))
            logging_thread = threading.Thread(target=emit_event)
            try:
                logging_thread.start()
                self.assertTrue(attempted.wait(1))
                self.assertFalse(shifted.wait(0.2))
                self.assertFalse((context.run_dir / "backend.jsonl.1").exists())
            finally:
                release.set()
                logging_thread.join(timeout=2)
                holder.join(timeout=2)
                for configured_handler in logger.handlers[:]:
                    logger.removeHandler(configured_handler)
                    configured_handler.close()
            self.assertFalse(logging_thread.is_alive())
            self.assertEqual(holder.exitcode, 0)
            self.assertEqual(errors, [])
            self.assertTrue(shifted.is_set())
            self.assertTrue((context.run_dir / "backend.jsonl.1").exists())


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

    def test_symlinked_log_dir_preserves_current_and_latest_run_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            container = Path(directory)
            physical_root = container / "physical-logs"
            physical_root.mkdir()
            linked_root = container / "linked-logs"
            linked_root.symlink_to(physical_root, target_is_directory=True)
            settings = replace(
                LoggingSettings(),
                log_dir=linked_root,
                retention_days=14,
                total_max_bytes=50,
            )
            latest_context = create_run_context(
                settings,
                now=datetime(2026, 8, 25, tzinfo=timezone.utc),
            )
            current = physical_root / "runs" / "2026-08-26T000000Z"
            current.mkdir()
            (latest_context.run_dir / "backend.jsonl").write_bytes(b"l" * 100)
            (current / "backend.jsonl").write_bytes(b"c" * 100)

            actions = cleanup_logs(
                settings,
                current,
                now=datetime(2026, 8, 26, tzinfo=timezone.utc),
            )

            self.assertTrue(latest_context.run_dir.is_dir())
            self.assertTrue(current.is_dir())
            self.assertEqual((linked_root / "latest").resolve(), latest_context.run_dir.resolve())
            self.assertFalse(any(action.bytes_removed for action in actions))

    def test_reports_size_cap_hard_floor_when_only_protected_files_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(LoggingSettings(), log_dir=root, total_max_bytes=50)
            active = create_run_context(settings).run_dir
            base = active / "backend.jsonl"
            manifest = active / "manifest.json"
            base.write_bytes(b"b" * 100)
            manifest.write_bytes(b"m" * 25)

            actions = cleanup_logs(settings, active)

            self.assertTrue(base.exists())
            self.assertTrue(manifest.exists())
            self.assertTrue(actions, "expected a hard-floor cleanup action")
            self.assertEqual(actions[-1].reason, "size_cap_hard_floor")
            self.assertEqual(actions[-1].bytes_removed, 0)

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

    def test_cleanup_tolerates_non_object_active_manifests(self):
        for manifest_value in ([], ["running"], "running", 42, True, None):
            with self.subTest(manifest_value=manifest_value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = replace(LoggingSettings(), log_dir=root, retention_days=1)
                malformed = root / "runs" / "2026-01-01T000000Z"
                malformed.mkdir(parents=True)
                (malformed / "manifest.json").write_text(
                    json.dumps(manifest_value), encoding="utf-8"
                )
                current = create_run_context(
                    settings, now=datetime(2026, 8, 26, tzinfo=timezone.utc)
                ).run_dir

                actions = cleanup_logs(
                    settings,
                    current,
                    now=datetime(2026, 8, 26, tzinfo=timezone.utc),
                )

                self.assertFalse(malformed.exists())
                self.assertTrue(any(action.reason == "expired" for action in actions))

    def test_deletes_expired_running_manifest_without_a_live_recorded_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(LoggingSettings(), log_dir=root, retention_days=1)
            abandoned = root / "runs" / "2026-01-01T000000Z"
            abandoned.mkdir(parents=True)
            (abandoned / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "running",
                        "supervisor_pid": 41001,
                        "children": {"backend": {"pid": -1}, "frontend": {"pid": "oops"}},
                    }
                ),
                encoding="utf-8",
            )
            current = create_run_context(
                settings, now=datetime(2026, 8, 26, tzinfo=timezone.utc)
            ).run_dir

            def fake_kill(pid, signal_number):
                self.assertEqual(signal_number, 0)
                raise ProcessLookupError(pid)

            with patch("backend.logging_config.os.kill", side_effect=fake_kill):
                actions = cleanup_logs(
                    settings,
                    current,
                    now=datetime(2026, 8, 26, tzinfo=timezone.utc),
                )

            self.assertFalse(abandoned.exists())
            self.assertTrue(
                any(action.path == str(abandoned) and action.reason == "expired" for action in actions)
            )

    def test_preserves_another_run_that_is_still_running_with_a_live_recorded_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(LoggingSettings(), log_dir=root, retention_days=1)
            first = create_run_context(
                settings, now=datetime(2026, 1, 1, tzinfo=timezone.utc)
            )
            (first.run_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "running",
                        "supervisor_pid": -1,
                        "children": {"frontend": {"pid": 52002}},
                    }
                ),
                encoding="utf-8",
            )
            second = create_run_context(
                settings, now=datetime(2026, 8, 26, tzinfo=timezone.utc)
            )
            def fake_kill(pid, signal_number):
                self.assertEqual(signal_number, 0)
                if pid == 52002:
                    raise PermissionError(pid)
                raise ProcessLookupError(pid)

            with patch("backend.logging_config.os.kill", side_effect=fake_kill):
                actions = cleanup_logs(
                    settings,
                    second.run_dir,
                    now=datetime(2026, 8, 26, tzinfo=timezone.utc),
                )
            self.assertTrue(first.run_dir.exists())
            self.assertFalse(any(action.path == str(first.run_dir) for action in actions))


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
