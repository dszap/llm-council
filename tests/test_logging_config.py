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

    def test_log_event_redacts_token_shaped_messages_before_handlers_receive_them(self):
        records = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("test.logging.redaction")
        logger.handlers = [CaptureHandler()]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        log_event(logger, logging.INFO, "test.completed", "Bearer secret-value")
        self.assertNotIn("secret-value", records[0].getMessage())


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
