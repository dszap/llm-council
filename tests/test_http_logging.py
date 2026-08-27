import io
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend import main
from backend.logging_config import JsonLineFormatter, log_event
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

    def test_unhandled_failure_returns_request_id(self):
        request_id = "28e8f443-7eb8-41e4-8ca6-79689b13d36d"
        logger = logging.getLogger("llm_council.http")
        original_handlers = logger.handlers[:]
        original_level = logger.level
        original_propagate = logger.propagate
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        try:
            with patch(
                "backend.main.storage.list_conversations", side_effect=RuntimeError("boom")
            ):
                response = TestClient(app, raise_server_exceptions=False).get(
                    "/api/conversations", headers={"X-Request-ID": request_id}
                )
        finally:
            logger.handlers = original_handlers
            logger.setLevel(original_level)
            logger.propagate = original_propagate
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["x-request-id"], request_id)

    def test_browser_batch_is_logged_with_server_owned_source(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter(source="browser", run_id="test-run"))
        logger = logging.getLogger("llm_council.browser")
        original_handlers = logger.handlers[:]
        original_level = logger.level
        original_propagate = logger.propagate
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
                            "details": {
                                "source": "attacker-controlled",
                                "nested": {"authorization": "Bearer secret-value"},
                            },
                        }
                    ]
                },
            )
        finally:
            logger.handlers = original_handlers
            logger.setLevel(original_level)
            logger.propagate = original_propagate
        self.assertEqual(response.status_code, 202)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["source"], "browser")
        self.assertEqual(payload["level"], "ERROR")
        self.assertEqual(payload["event"], "browser.unhandled_error")
        self.assertEqual(payload["details"]["source"], "attacker-controlled")
        self.assertEqual(payload["details"]["nested"]["authorization"], "[REDACTED]")
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

    def test_conversation_route_binds_request_and_conversation_ids(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter(run_id="test-run"))
        logger = logging.getLogger("llm_council.backend")
        original_handlers = logger.handlers[:]
        original_level = logger.level
        original_propagate = logger.propagate
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        async def fake_council(user_query):
            log_event(logger, logging.INFO, "test.council", "Council called")
            return [], [], {"model": "test/model", "response": "ok"}, {}

        conversation_id = "13c799b4-0d8f-42b9-9b7d-7c2ed3d478d7"
        request_id = "28e8f443-7eb8-41e4-8ca6-79689b13d36d"
        try:
            with patch(
                "backend.main.storage.get_conversation", return_value={"messages": [{}]}
            ), patch("backend.main.storage.add_user_message"), patch(
                "backend.main.storage.add_assistant_message"
            ), patch(
                "backend.main.run_full_council", new=AsyncMock(side_effect=fake_council)
            ):
                response = self.client.post(
                    f"/api/conversations/{conversation_id}/message",
                    headers={"X-Request-ID": request_id},
                    json={"content": "question"},
                )
        finally:
            logger.handlers = original_handlers
            logger.setLevel(original_level)
            logger.propagate = original_propagate
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
        original_level = logger.level
        original_propagate = logger.propagate
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        async def fake_stage1(user_query):
            log_event(logger, logging.INFO, "test.stream.stage1", "Stage called")
            return []

        conversation_id = "13c799b4-0d8f-42b9-9b7d-7c2ed3d478d7"
        request_id = "28e8f443-7eb8-41e4-8ca6-79689b13d36d"
        try:
            with patch(
                "backend.main.storage.get_conversation", return_value={"messages": [{}]}
            ), patch("backend.main.storage.add_user_message"), patch(
                "backend.main.storage.add_assistant_message"
            ), patch(
                "backend.main.stage1_collect_responses",
                new=AsyncMock(side_effect=fake_stage1),
            ), patch(
                "backend.main.stage2_collect_rankings", new=AsyncMock(return_value=([], {}))
            ), patch(
                "backend.main.stage3_synthesize_final",
                new=AsyncMock(return_value={"model": "test/model", "response": "ok"}),
            ):
                with self.client.stream(
                    "POST",
                    f"/api/conversations/{conversation_id}/message/stream",
                    headers={"X-Request-ID": request_id},
                    json={"content": "question"},
                ) as response:
                    body = "".join(response.iter_text())
        finally:
            logger.handlers = original_handlers
            logger.setLevel(original_level)
            logger.propagate = original_propagate
        self.assertIn('"type": "complete"', body)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["request_id"], request_id)
        self.assertEqual(payload["conversation_id"], conversation_id)

    def test_create_conversation_binds_request_and_generated_conversation_ids(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter(run_id="test-run"))
        logger = logging.getLogger("llm_council.backend")
        original_handlers = logger.handlers[:]
        original_level = logger.level
        original_propagate = logger.propagate
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        def create_and_log(conversation_id):
            log_event(logger, logging.INFO, "test.conversation.created", "Created")
            return {
                "id": conversation_id,
                "created_at": "2026-08-27T00:00:00Z",
                "title": "New Conversation",
                "messages": [],
            }

        request_id = "28e8f443-7eb8-41e4-8ca6-79689b13d36d"
        try:
            with patch(
                "backend.main.storage.create_conversation", side_effect=create_and_log
            ):
                response = self.client.post(
                    "/api/conversations",
                    headers={"X-Request-ID": request_id},
                    json={},
                )
        finally:
            logger.handlers = original_handlers
            logger.setLevel(original_level)
            logger.propagate = original_propagate
        self.assertEqual(response.status_code, 200)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["request_id"], request_id)
        self.assertEqual(payload["conversation_id"], response.json()["id"])

    def test_logging_setup_falls_back_when_delayed_file_open_fails(self):
        logger_names = (
            "llm_council.backend",
            "llm_council.browser",
            "llm_council.http",
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
        )
        logger_state = {
            name: (
                logging.getLogger(name).handlers[:],
                logging.getLogger(name).level,
                logging.getLogger(name).propagate,
            )
            for name in logger_names
        }
        original_backend = main.backend_logger
        original_browser = main.browser_logger
        original_http = main.http_logger
        original_raise_exceptions = logging.raiseExceptions
        logging.raiseExceptions = False
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ,
                {
                    "LLM_COUNCIL_RUN_DIR": directory,
                    "LLM_COUNCIL_RUN_ID": "test-run",
                },
                clear=False,
            ), patch(
                "backend.logging_config._CleanupRotatingFileHandler._open",
                side_effect=OSError("write denied"),
            ), patch("backend.main.log_event"):
                main._configure_logging()
                self.assertFalse(
                    any(
                        isinstance(handler, logging.FileHandler)
                        for handler in main.backend_logger.handlers
                    )
                )
                self.assertTrue(
                    any(
                        isinstance(handler, logging.StreamHandler)
                        for handler in main.backend_logger.handlers
                    )
                )
        finally:
            logging.raiseExceptions = original_raise_exceptions
            for name, (handlers, level, propagate) in logger_state.items():
                logger = logging.getLogger(name)
                for handler in logger.handlers[:]:
                    if handler not in handlers:
                        logger.removeHandler(handler)
                        handler.close()
                logger.handlers = handlers
                logger.setLevel(level)
                logger.propagate = propagate
            main.backend_logger = original_backend
            main.browser_logger = original_browser
            main.http_logger = original_http
