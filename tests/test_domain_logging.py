import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from backend import council, main, openrouter


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
                    "provider/model", [{"role": "user", "content": "x"}]
                )
        finally:
            logger.handlers = original_handlers
        self.assertIsNone(result)
        self.assertEqual(
            [record.event_name for record in records],
            ["openrouter.request.started", "openrouter.request.failed"],
        )
        self.assertEqual(records[-1].event_fields["status_code"], 402)

    async def test_malformed_response_logs_malformed_category(self):
        fake_http_response = Mock()
        fake_http_response.raise_for_status.return_value = None
        fake_http_response.json.return_value = {"choices": [{"message": "not an object"}]}
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
                    "provider/model", [{"role": "user", "content": "x"}]
                )
        finally:
            logger.handlers = original_handlers
        self.assertIsNone(result)
        self.assertEqual(records[-1].event_name, "openrouter.request.failed")
        self.assertEqual(records[-1].event_fields["error_category"], "malformed_response")


class CouncilLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_domain_events_use_backend_jsonl_only(self):
        logger_names = (
            "llm_council",
            "llm_council.backend",
            "llm_council.browser",
            "llm_council.http",
            "llm_council.council",
            "llm_council.openrouter",
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
        )
        states = {
            name: (
                logging.getLogger(name).handlers[:],
                logging.getLogger(name).level,
                logging.getLogger(name).propagate,
            )
            for name in logger_names
        }
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ,
                {"LLM_COUNCIL_RUN_DIR": directory, "LLM_COUNCIL_RUN_ID": "runtime-run"},
                clear=False,
            ), patch(
                "backend.council.query_models_parallel",
                new=AsyncMock(return_value={"model/a": {"content": "answer"}}),
            ):
                main._configure_logging()
                await council.stage1_collect_responses("question")
                backend_events = [
                    json.loads(line)
                    for line in Path(directory, "backend.jsonl").read_text().splitlines()
                ]
                browser_events = [
                    json.loads(line)
                    for line in Path(directory, "browser.jsonl").read_text().splitlines()
                ]
            domain_events = [
                event for event in backend_events if event["logger"] == "llm_council.council"
            ]
            self.assertTrue(domain_events)
            self.assertTrue(all(event["source"] == "backend" for event in domain_events))
            self.assertFalse(
                any(event["logger"] in {"llm_council.council", "llm_council.openrouter"}
                    for event in browser_events)
            )
        finally:
            for name, (handlers, level, propagate) in states.items():
                logger = logging.getLogger(name)
                for handler in logger.handlers[:]:
                    if handler not in handlers:
                        logger.removeHandler(handler)
                        handler.close()
                logger.handlers = handlers
                logger.setLevel(level)
                logger.propagate = propagate

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
            with patch.dict("os.environ", {"LOG_LLM_PAYLOADS": "true"}), patch(
                "backend.openrouter.httpx.AsyncClient", return_value=fake_context
            ):
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
