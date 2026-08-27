"""FastAPI backend for LLM Council."""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import storage
from .council import (
    calculate_aggregate_rankings,
    generate_conversation_title,
    run_full_council,
    stage1_collect_responses,
    stage2_collect_rankings,
    stage3_synthesize_final,
)
from .logging_config import (
    ConsoleFormatter,
    LoggingSettings,
    bind_log_context,
    configure_source_logger,
    create_run_context,
    log_event,
    reset_log_context,
)

app = FastAPI(title="LLM Council API")
ALLOWED_FRONTEND_ORIGINS = ("http://localhost:5173", "http://localhost:3000")
BROWSER_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

backend_logger = logging.getLogger("llm_council.backend")
browser_logger = logging.getLogger("llm_council.browser")
http_logger = logging.getLogger("llm_council.http")

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def valid_request_id(value: str | None) -> str:
    """Return a valid incoming request ID or generate a new UUID."""
    if value:
        try:
            uuid.UUID(value)
            return value
        except (TypeError, ValueError, AttributeError):
            pass
    return str(uuid.uuid4())


@app.middleware("http")
async def correlate_request(request: Request, call_next):
    """Correlate HTTP request logs without recording request content or headers."""
    request_id = valid_request_id(request.headers.get("X-Request-ID"))
    request.state.request_id = request_id
    tokens = bind_log_context(request_id, None)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        log_event(
            http_logger,
            logging.INFO,
            "http.request.completed",
            "HTTP request completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception:
        log_event(
            http_logger,
            logging.ERROR,
            "http.request.failed",
            "HTTP request failed",
            method=request.method,
            path=request.url.path,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        reset_log_context(tokens)


class CreateConversationRequest(BaseModel):
    """Request to create a new conversation."""
    pass


class SendMessageRequest(BaseModel):
    """Request to send a message in a conversation."""
    content: str


class ConversationMetadata(BaseModel):
    """Conversation metadata for list view."""
    id: str
    created_at: str
    title: str
    message_count: int


class Conversation(BaseModel):
    """Full conversation with all messages."""
    id: str
    created_at: str
    title: str
    messages: List[Dict[str, Any]]


class BrowserLogEvent(BaseModel):
    """A bounded, client-supplied browser log event."""

    model_config = ConfigDict(extra="forbid")

    client_timestamp: str = Field(min_length=1, max_length=64)
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    event: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4096)
    browser_session_id: str = Field(min_length=1, max_length=128)
    page: str = Field(min_length=1, max_length=2048)
    details: Dict[str, Any] | None = Field(default=None, max_length=32)


class BrowserLogBatch(BaseModel):
    """A bounded browser log batch that is safe to validate before logging."""

    model_config = ConfigDict(extra="forbid")

    events: List[BrowserLogEvent] = Field(min_length=1, max_length=100)


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"status": "ok", "service": "LLM Council API"}


@app.post("/api/logs/browser", status_code=202)
async def ingest_browser_logs(batch: BrowserLogBatch, request: Request):
    """Accept bounded browser events from configured local development origins."""
    origin = request.headers.get("origin")
    if origin not in ALLOWED_FRONTEND_ORIGINS:
        raise HTTPException(status_code=403, detail="Origin not allowed")

    settings = LoggingSettings.from_env()
    if len(batch.events) > settings.browser_batch_size:
        raise HTTPException(status_code=413, detail="Browser log batch too large")
    if len(batch.model_dump_json().encode("utf-8")) > settings.event_max_bytes:
        raise HTTPException(status_code=413, detail="Browser log batch too large")

    for event in batch.events:
        log_event(
            browser_logger,
            BROWSER_LEVELS[event.level],
            event.event,
            event.message,
            client_timestamp=event.client_timestamp,
            browser_session_id=event.browser_session_id,
            page=event.page,
            details=event.details or {},
        )
    return {"accepted": len(batch.events)}


@app.get("/api/conversations", response_model=List[ConversationMetadata])
async def list_conversations():
    """List all conversations (metadata only)."""
    return storage.list_conversations()


@app.post("/api/conversations", response_model=Conversation)
async def create_conversation(request: CreateConversationRequest, http_request: Request):
    """Create a new conversation."""
    conversation_id = str(uuid.uuid4())
    tokens = bind_log_context(http_request.state.request_id, conversation_id)
    try:
        return storage.create_conversation(conversation_id)
    finally:
        reset_log_context(tokens)


@app.get("/api/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(conversation_id: str, request: Request):
    """Get a specific conversation with all its messages."""
    tokens = bind_log_context(request.state.request_id, conversation_id)
    try:
        conversation = storage.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conversation
    finally:
        reset_log_context(tokens)


@app.post("/api/conversations/{conversation_id}/message")
async def send_message(
    conversation_id: str, request: SendMessageRequest, http_request: Request
):
    """
    Send a message and run the 3-stage council process.
    Returns the complete response with all stages.
    """
    tokens = bind_log_context(http_request.state.request_id, conversation_id)
    try:
        # Check if conversation exists
        conversation = storage.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Check if this is the first message
        is_first_message = len(conversation["messages"]) == 0

        # Add user message
        storage.add_user_message(conversation_id, request.content)

        # If this is the first message, generate a title
        if is_first_message:
            title = await generate_conversation_title(request.content)
            storage.update_conversation_title(conversation_id, title)

        # Run the 3-stage council process
        stage1_results, stage2_results, stage3_result, metadata = await run_full_council(
            request.content
        )

        # Add assistant message with all stages
        storage.add_assistant_message(
            conversation_id,
            stage1_results,
            stage2_results,
            stage3_result,
        )

        # Return the complete response with metadata
        return {
            "stage1": stage1_results,
            "stage2": stage2_results,
            "stage3": stage3_result,
            "metadata": metadata,
        }
    finally:
        reset_log_context(tokens)


@app.post("/api/conversations/{conversation_id}/message/stream")
async def send_message_stream(
    conversation_id: str, request: SendMessageRequest, http_request: Request
):
    """
    Send a message and stream the 3-stage council process.
    Returns Server-Sent Events as each stage completes.
    """
    tokens = bind_log_context(http_request.state.request_id, conversation_id)
    try:
        # Check if conversation exists
        conversation = storage.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Check if this is the first message
        is_first_message = len(conversation["messages"]) == 0
    finally:
        reset_log_context(tokens)

    async def event_generator():
        generator_tokens = bind_log_context(http_request.state.request_id, conversation_id)
        try:
            # Add user message
            storage.add_user_message(conversation_id, request.content)

            # Start title generation in parallel (don't await yet)
            title_task = None
            if is_first_message:
                title_task = asyncio.create_task(generate_conversation_title(request.content))

            # Stage 1: Collect responses
            yield f"data: {json.dumps({'type': 'stage1_start'})}\n\n"
            stage1_results = await stage1_collect_responses(request.content)
            yield f"data: {json.dumps({'type': 'stage1_complete', 'data': stage1_results})}\n\n"

            # Stage 2: Collect rankings
            yield f"data: {json.dumps({'type': 'stage2_start'})}\n\n"
            stage2_results, label_to_model = await stage2_collect_rankings(request.content, stage1_results)
            aggregate_rankings = calculate_aggregate_rankings(stage2_results, label_to_model)
            yield f"data: {json.dumps({'type': 'stage2_complete', 'data': stage2_results, 'metadata': {'label_to_model': label_to_model, 'aggregate_rankings': aggregate_rankings}})}\n\n"

            # Stage 3: Synthesize final answer
            yield f"data: {json.dumps({'type': 'stage3_start'})}\n\n"
            stage3_result = await stage3_synthesize_final(request.content, stage1_results, stage2_results)
            yield f"data: {json.dumps({'type': 'stage3_complete', 'data': stage3_result})}\n\n"

            # Wait for title generation if it was started
            if title_task:
                title = await title_task
                storage.update_conversation_title(conversation_id, title)
                yield f"data: {json.dumps({'type': 'title_complete', 'data': {'title': title}})}\n\n"

            # Save complete assistant message
            storage.add_assistant_message(
                conversation_id,
                stage1_results,
                stage2_results,
                stage3_result
            )

            # Send completion event
            yield f"data: {json.dumps({'type': 'complete'})}\n\n"

        except Exception as e:
            # Send error event
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            reset_log_context(generator_tokens)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


def _configure_console_logger(name: str, level: str) -> logging.Logger:
    """Configure a source logger that remains useful when file logging fails."""
    logger = logging.getLogger(name)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(ConsoleFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def _validate_file_targets(*loggers: logging.Logger) -> None:
    """Open and flush delayed file targets while setup errors can still recover."""
    for logger in loggers:
        for handler in logger.handlers:
            if not isinstance(handler, logging.FileHandler):
                continue
            handler.acquire()
            try:
                if handler.stream is None:
                    handler.stream = handler._open()
                handler.stream.write("")
                handler.stream.flush()
            finally:
                handler.release()


def _close_logger_handlers(*names: str) -> None:
    """Remove and close partially configured handlers without double-closing."""
    closed_handlers: set[int] = set()
    for name in names:
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler_id = id(handler)
            if handler_id not in closed_handlers:
                handler.close()
                closed_handlers.add(handler_id)


def _configure_logging() -> None:
    """Configure separate backend, browser, and Uvicorn JSONL log sources."""
    global backend_logger, browser_logger

    settings = LoggingSettings.from_env()
    try:
        run_dir_value = os.environ.get("LLM_COUNCIL_RUN_DIR")
        run_id = os.environ.get("LLM_COUNCIL_RUN_ID")
        if run_dir_value and run_id:
            run_dir = Path(run_dir_value)
        else:
            context = create_run_context(settings)
            run_dir = context.run_dir
            run_id = context.run_id

        backend_logger = configure_source_logger(
            "llm_council.backend",
            "backend",
            run_dir / "backend.jsonl",
            settings.backend_level,
            run_id,
            settings,
            include_console=True,
        )
        browser_logger = configure_source_logger(
            "llm_council.browser",
            "browser",
            run_dir / "browser.jsonl",
            settings.browser_level,
            run_id,
            settings,
            include_console=True,
        )
        uvicorn_logger = configure_source_logger(
            "uvicorn",
            "uvicorn",
            run_dir / "uvicorn.jsonl",
            settings.uvicorn_level,
            run_id,
            settings,
            include_console=True,
        )
        _validate_file_targets(backend_logger, browser_logger, uvicorn_logger)
        http_logger.handlers = backend_logger.handlers[:]
        http_logger.setLevel(settings.backend_level)
        http_logger.propagate = False
        for name in ("uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            logger.handlers = []
            logger.setLevel(settings.uvicorn_level)
            logger.propagate = True
        uvicorn_logger.propagate = False
    except OSError:
        _close_logger_handlers(
            "llm_council.backend",
            "llm_council.browser",
            "llm_council.http",
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
        )
        backend_logger = _configure_console_logger(
            "llm_council.backend", settings.backend_level
        )
        browser_logger = _configure_console_logger(
            "llm_council.browser", settings.browser_level
        )
        uvicorn_logger = _configure_console_logger("uvicorn", settings.uvicorn_level)
        http_logger.handlers = backend_logger.handlers[:]
        http_logger.setLevel(settings.backend_level)
        http_logger.propagate = False
        for name in ("uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            logger.handlers = []
            logger.setLevel(settings.uvicorn_level)
            logger.propagate = True
        uvicorn_logger.propagate = False
    log_event(
        backend_logger,
        logging.INFO,
        "backend.started",
        "Backend started",
        settings=settings.to_safe_dict(),
    )


if __name__ == "__main__":
    import uvicorn

    _configure_logging()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        log_config=None,
        access_log=True,
    )
