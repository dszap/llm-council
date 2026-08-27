import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_child_environment_contains_run_context_but_no_vite_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = LoggingSettings(log_dir=Path(directory))
            context = create_run_context(settings)
            with patch.dict("os.environ", {"OPENROUTER_API_KEY": "secret-marker"}):
                environment = build_child_environment(settings, context)
            self.assertEqual(environment["LLM_COUNCIL_RUN_ID"], context.run_id)
            self.assertEqual(
                environment["LLM_COUNCIL_RUN_DIR"], str(context.run_dir.resolve())
            )
            vite_values = {
                key: value
                for key, value in environment.items()
                if key.startswith("VITE_")
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
