import io
import json
import logging
import unittest
from pathlib import Path

from backend.logging_config import (
    ConsoleFormatter,
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

    def test_event_limit_below_minimum_falls_back_safely(self):
        warnings = []
        settings = LoggingSettings.from_env(
            {"LOG_EVENT_MAX_BYTES": "1"}, warning_sink=warnings.append
        )
        self.assertEqual(settings.event_max_bytes, 65536)
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("1", warnings[0])

    def test_payload_logging_only_accepts_literal_true(self):
        warnings = []
        for value in ("1", "yes", "on"):
            settings = LoggingSettings.from_env(
                {"LOG_LLM_PAYLOADS": value}, warning_sink=warnings.append
            )
            self.assertFalse(settings.log_llm_payloads)
        self.assertEqual(len(warnings), 3)
        self.assertTrue(all(value not in " ".join(warnings) for value in ("1", "yes", "on")))
        self.assertTrue(LoggingSettings.from_env({"LOG_LLM_PAYLOADS": "TRUE"}).log_llm_payloads)


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

    def test_redacts_separator_variants_and_complete_cookie_headers(self):
        value = {
            "Proxy Authorization": "Bearer proxy-secret",
            "api-key": "api-secret",
            "Cookie": "session=one; csrf=two",
            "Set-Cookie": "sid=three; theme=four",
        }
        result = redact(value)
        self.assertEqual(result["Proxy Authorization"], "[REDACTED]")
        self.assertEqual(result["api-key"], "[REDACTED]")
        self.assertEqual(redact("Cookie: session=one; csrf=two"), "Cookie: [REDACTED]")
        self.assertEqual(redact("Set-Cookie: sid=three; theme=four"), "Set-Cookie: [REDACTED]")

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

    def test_redacts_inline_credentials_from_messages_exceptions_and_formatters(self):
        secret_values = (
            "pass-value",
            "cookie-value",
            "basic-value",
            "key-value",
            "secret-value",
            "top-secret",
        )
        text = (
            "password=pass-value Cookie: cookie-value Authorization: Basic basic-value "
            "api_key=key-value secret: secret-value api key: top-secret"
        )
        self.assertTrue(all(value not in redact(text) for value in secret_values))

        json_stream = io.StringIO()
        console_stream = io.StringIO()
        logger = logging.getLogger("test.logging.inline_credentials")
        logger.handlers = [
            logging.StreamHandler(json_stream),
            logging.StreamHandler(console_stream),
        ]
        logger.handlers[0].setFormatter(JsonLineFormatter())
        logger.handlers[1].setFormatter(ConsoleFormatter())
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        try:
            raise RuntimeError(text)
        except RuntimeError:
            logger.exception(text)

        combined = json_stream.getvalue() + console_stream.getvalue()
        self.assertTrue(all(value not in combined for value in secret_values))
        json.loads(json_stream.getvalue())

    def test_json_formatter_normalizes_non_finite_floats(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter())
        logger = logging.getLogger("test.logging.non_finite")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        log_event(
            logger,
            logging.INFO,
            "metrics.completed",
            "Measured",
            nan=float("nan"),
            positive=float("inf"),
            negative=float("-inf"),
        )
        payload = json.loads(stream.getvalue())
        self.assertIsNone(payload["nan"])
        self.assertIsNone(payload["positive"])
        self.assertIsNone(payload["negative"])
        self.assertNotIn("NaN", stream.getvalue())

    def test_json_formatter_truncates_every_serialized_string(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter(run_id="run-identifier", event_max_bytes=8))
        logger = logging.getLogger("test.logging.truncation")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        tokens = bind_log_context(
            request_id="request-identifier", conversation_id="conversation-identifier"
        )
        try:
            try:
                raise RuntimeError("exception-is-longer-than-eight-bytes")
            except RuntimeError:
                logger.exception(
                    "message-is-longer-than-eight-bytes",
                    extra={
                        "event_name": "event-is-longer-than-eight-bytes",
                        "event_fields": {
                            "outer": {"nested": "nested-is-longer-than-eight-bytes"},
                            "items": ["item-is-longer-than-eight-bytes"],
                            "unbounded-field-name": {
                                "unbounded-nested-name": "nested-value-is-longer-than-eight-bytes"
                            },
                        },
                    },
                )
        finally:
            reset_log_context(tokens)

        serialized = stream.getvalue().rstrip("\n")
        self.assertLessEqual(len(serialized.encode("utf-8")), 8)
        json.loads(serialized)

    def test_json_formatter_preserves_named_metadata_when_it_fits(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter(event_max_bytes=64))
        logger = logging.getLogger("test.logging.named_metadata")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        log_event(logger, logging.INFO, "test.completed", "Done", answer="a" * 65)

        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["answer_truncated"])
        self.assertEqual(payload["answer_original_bytes"], 65)

    def test_json_formatter_caps_complete_serialized_payload(self):
        for max_bytes in (128, 256, 512):
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            handler.setFormatter(JsonLineFormatter(event_max_bytes=max_bytes))
            logger = logging.getLogger(f"test.logging.aggregate_limit.{max_bytes}")
            logger.handlers = [handler]
            logger.propagate = False
            logger.setLevel(logging.INFO)
            log_event(
                logger,
                logging.INFO,
                "oversized.event",
                "m" * 500,
                **{f"field_{index}": "x" * 500 for index in range(20)},
            )
            serialized = stream.getvalue().rstrip("\n")
            self.assertLessEqual(len(serialized.encode("utf-8")), max_bytes)
            json.loads(serialized)

    def test_json_formatter_caps_complete_payload_at_small_supported_limits(self):
        for max_bytes in (2, 8, 14, 16, 24, 32, 63):
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            handler.setFormatter(JsonLineFormatter(event_max_bytes=max_bytes))
            logger = logging.getLogger(f"test.logging.small_limit.{max_bytes}")
            logger.handlers = [handler]
            logger.propagate = False
            logger.setLevel(logging.INFO)
            log_event(logger, logging.INFO, "oversized.event", "x" * 500)
            serialized = stream.getvalue().rstrip("\n")
            self.assertLessEqual(len(serialized.encode("utf-8")), max_bytes)
            payload = json.loads(serialized)
            if max_bytes in (14, 16, 24):
                self.assertIn("message", payload)
                self.assertTrue(("x" * 500).startswith(payload["message"]))

        with self.assertRaises(ValueError):
            JsonLineFormatter(event_max_bytes=1)


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
