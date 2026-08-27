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
                rotated_paths = sorted(
                    (
                        candidate
                        for candidate in path.parent.glob(f"{name}.*")
                        if candidate.name.removeprefix(f"{name}.").isdigit()
                    ),
                    key=lambda candidate: int(candidate.name.removeprefix(f"{name}.")),
                )
                events[name] = [
                    json.loads(line)
                    for source_path in [path, *rotated_paths]
                    for line in source_path.read_text().splitlines()
                    if line
                ]
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
