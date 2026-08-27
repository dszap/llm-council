import contextlib
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend import dev_runner
from backend.dev_runner import (
    atomic_write_manifest,
    build_child_commands,
    build_child_environment,
    run,
    stop_children,
    stream_vite_output,
)
from backend.logging_config import LoggingSettings, create_run_context


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
    def test_loads_logging_settings_from_repository_env_before_start(self, popen):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_dir = root / "configured-logs"
            (root / ".env").write_text(
                f"LOG_DIR={log_dir}\nLOG_BACKEND_LEVEL=DEBUG\n", encoding="utf-8"
            )
            backend = Mock(pid=101)
            frontend = Mock(pid=102)
            backend.poll.side_effect = [None, 1]
            frontend.poll.return_value = None
            frontend.stdout = iter([])
            popen.side_effect = [backend, frontend]
            with patch.object(dev_runner, "REPO_ROOT", root), patch.dict(
                "os.environ", {}, clear=False
            ):
                os.environ.pop("LOG_DIR", None)
                os.environ.pop("LOG_BACKEND_LEVEL", None)
                result = run(settings=None, poll_interval=0)
            self.assertNotEqual(result, 0)
            self.assertTrue((log_dir / "latest").exists())
            manifest = json.loads(next(log_dir.glob("runs/*/manifest.json")).read_text())
            self.assertEqual(manifest["settings"]["backend_level"], "DEBUG")

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
            self.assertEqual(manifest["repository_root"], str(dev_runner.REPO_ROOT.resolve()))
            self.assertEqual(manifest["children"]["backend"]["cwd"], str(dev_runner.REPO_ROOT.resolve()))
            self.assertEqual(manifest["children"]["frontend"]["cwd"], str((dev_runner.REPO_ROOT / "frontend").resolve()))
            self.assertEqual(manifest["services"]["backend"], {"host": "0.0.0.0", "port": 8001})
            self.assertEqual(manifest["services"]["frontend"], {"host": "localhost", "port": 5173})

    @patch("backend.dev_runner.subprocess.Popen")
    def test_startup_cleanup_failure_warns_redacted_but_starts_children(self, popen):
        with tempfile.TemporaryDirectory() as directory:
            settings = LoggingSettings(log_dir=Path(directory))
            backend = Mock(pid=101)
            frontend = Mock(pid=102)
            backend.poll.side_effect = [None, 1]
            frontend.poll.return_value = None
            frontend.wait.return_value = 0
            frontend.stdout = iter([])
            popen.side_effect = [backend, frontend]
            terminal = io.StringIO()
            with patch(
                "backend.dev_runner.cleanup_logs",
                side_effect=[OSError("OPENROUTER_API_KEY=cleanup-secret"), []],
            ), contextlib.redirect_stderr(terminal):
                try:
                    result = run(settings=settings, poll_interval=0)
                except Exception as error:  # Establish RED without letting the test error.
                    result = error

            self.assertEqual(result, 1)
            self.assertEqual(popen.call_count, 2)
            manifest_text = next(Path(directory).glob("runs/*/manifest.json")).read_text()
            self.assertNotIn("cleanup-secret", terminal.getvalue())
            self.assertNotIn("cleanup-secret", manifest_text)

    @patch("backend.dev_runner.subprocess.Popen")
    def test_manifest_write_failure_is_redacted_and_later_finalization_continues(self, popen):
        with tempfile.TemporaryDirectory() as directory:
            settings = LoggingSettings(log_dir=Path(directory))
            backend = Mock(pid=101)
            frontend = Mock(pid=102)
            backend.poll.side_effect = [None, 1]
            frontend.poll.return_value = None
            frontend.wait.return_value = 0
            frontend.stdout = iter([])
            popen.side_effect = [backend, frontend]
            write_manifest = dev_runner.atomic_write_manifest
            calls = 0

            def fail_initial_manifest(path, payload):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("OPENROUTER_API_KEY=manifest-secret")
                write_manifest(path, payload)

            terminal = io.StringIO()
            with patch(
                "backend.dev_runner.atomic_write_manifest",
                side_effect=fail_initial_manifest,
            ), contextlib.redirect_stderr(terminal):
                try:
                    result = run(settings=settings, poll_interval=0)
                except Exception as error:  # Establish RED without letting the test error.
                    result = error

            self.assertEqual(result, 1)
            self.assertEqual(popen.call_count, 2)
            manifest_text = next(Path(directory).glob("runs/*/manifest.json")).read_text()
            self.assertIn('\"status\": \"unclean\"', manifest_text)
            self.assertNotIn("manifest-secret", terminal.getvalue())
            self.assertNotIn("manifest-secret", manifest_text)

    @patch("backend.dev_runner.subprocess.Popen")
    def test_shutdown_cleanup_failure_does_not_skip_finalization(self, popen):
        with tempfile.TemporaryDirectory() as directory:
            settings = LoggingSettings(log_dir=Path(directory))
            backend = Mock(pid=101)
            frontend = Mock(pid=102)
            backend.poll.side_effect = [None, 1]
            frontend.poll.return_value = None
            frontend.wait.return_value = 0
            frontend.stdout = iter([])
            popen.side_effect = [backend, frontend]
            terminal = io.StringIO()
            with patch(
                "backend.dev_runner.cleanup_logs",
                side_effect=[[], OSError("OPENROUTER_API_KEY=shutdown-secret")],
            ), contextlib.redirect_stderr(terminal):
                try:
                    result = run(settings=settings, poll_interval=0)
                except Exception as error:  # Establish RED without letting the test error.
                    result = error

            self.assertEqual(result, 1)
            manifest_text = next(Path(directory).glob("runs/*/manifest.json")).read_text()
            self.assertIn('\"status\": \"unclean\"', manifest_text)
            self.assertNotIn("shutdown-secret", terminal.getvalue())
            self.assertNotIn("shutdown-secret", manifest_text)

    @patch("backend.dev_runner.subprocess.Popen")
    def test_frontend_start_failure_stops_backend_once_and_sanitizes_manifest(
        self, popen
    ):
        with tempfile.TemporaryDirectory() as directory:
            settings = LoggingSettings(log_dir=Path(directory))
            backend = Mock(pid=101)
            backend.poll.return_value = None
            backend.wait.return_value = 0
            popen.side_effect = [
                backend,
                OSError("OPENROUTER_API_KEY=secret-marker"),
            ]

            result = run(settings=settings, poll_interval=0)

            self.assertNotEqual(result, 0)
            backend.terminate.assert_called_once()
            manifest_path = next(Path(directory).glob("runs/*/manifest.json"))
            manifest_text = manifest_path.read_text()
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["status"], "unclean")
            self.assertNotIn("secret-marker", manifest_text)


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

    def test_frontend_environment_is_allowlisted_and_contains_only_approved_vite_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = LoggingSettings(log_dir=Path(directory))
            context = create_run_context(settings)
            with patch.dict(
                "os.environ",
                {
                    "PATH": "/runtime/bin",
                    "HOME": "/runtime/home",
                    "TMPDIR": "/runtime/tmp",
                    "OPENROUTER_API_KEY": "openrouter-secret",
                    "ARBITRARY_SECRET": "arbitrary-secret",
                    "VITE_UNAPPROVED": "unapproved-value",
                },
                clear=True,
            ):
                environment = build_child_environment(settings, context)
            self.assertEqual(environment["PATH"], "/runtime/bin")
            self.assertEqual(environment["HOME"], "/runtime/home")
            self.assertEqual(environment["TMPDIR"], "/runtime/tmp")
            self.assertNotIn("OPENROUTER_API_KEY", environment)
            self.assertNotIn("ARBITRARY_SECRET", environment)
            self.assertNotIn("VITE_UNAPPROVED", environment)
            self.assertEqual(
                {key for key in environment if key.startswith("VITE_")},
                set(dev_runner.APPROVED_VITE_SETTINGS),
            )
            self.assertEqual(environment["VITE_LOG_BROWSER_BATCH_SIZE"], "20")


class ShutdownTests(unittest.TestCase):
    def test_sigint_shutdown_is_forwarded_as_sigint(self):
        child = Mock(pid=100)
        child.poll.return_value = None
        child.wait.return_value = -signal.SIGINT
        result = stop_children(
            [child], grace_seconds=0.1, shutdown_signal=signal.SIGINT
        )
        child.send_signal.assert_called_once_with(signal.SIGINT)
        child.terminate.assert_not_called()
        self.assertEqual(result[100], -signal.SIGINT)

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
