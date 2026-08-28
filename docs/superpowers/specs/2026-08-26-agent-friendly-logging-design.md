# Agent-Friendly Logging Design

**Date:** 2026-08-26
**Status:** Approved in chat; awaiting review of this written specification

## Purpose

LLM Council needs durable local logs that are simultaneously useful to a person watching the development terminal and to an agent debugging the application through the filesystem. The current `start.sh` interleaves backend and Vite output only in its launching terminal, backend OpenRouter errors use `print`, browser errors remain in the browser console, and no durable log history or cleanup policy exists.

This feature creates one logging lifecycle for a local application run. It preserves concise terminal output, writes structured source-specific files, correlates a user action across backend work, captures important browser failures, rotates large files, and removes expired logs.

## Goals

- Show backend application, Uvicorn, Vite, and forwarded browser events in the terminal used to run `start.sh`.
- Write each source to a separate JSON Lines file under a timestamped run directory.
- Give agents a stable `logs/latest` path for the active or most recent run.
- Correlate HTTP, council-stage, and OpenRouter events with request and conversation identifiers.
- Log LLM metadata by default and allow explicit opt-in logging of prompts and responses.
- Redact secrets regardless of payload settings.
- Rotate every active log at 10 MiB, retain logs for 14 days, and cap aggregate storage at 500 MiB.
- Capture important browser failures without making logging a dependency of application behavior.
- Use the Python standard library for backend logging and process supervision; add no runtime logging framework.

## Non-goals

- A log viewer inside the React application.
- Remote aggregation, cloud shipping, metrics, tracing backends, or alerting.
- Production deployment or multi-host logging.
- Persisting every browser `console.log` call at the default level.
- Logging authorization headers, cookies, API keys, or other credentials under any configuration.
- Changing council behavior, model selection, or conversation storage.

## Chosen Approach

Use Python standard-library logging plus a small Python development supervisor. `start.sh` becomes a minimal wrapper that executes the supervisor. The supervisor owns the run directory, manifest, child-process lifecycle, Vite output capture, and retention coordination. The FastAPI process owns structured backend, Uvicorn, and browser-ingestion handlers.

This approach avoids new runtime dependencies while providing stronger lifecycle and retention behavior than shell pipes. A dedicated collector or external logging framework would add infrastructure that is not justified for this local application.

## Architecture

```text
start.sh
  └─ backend.dev_runner
       ├─ creates logs/runs/<run-id>/ and logs/latest
       ├─ writes manifest.json
       ├─ starts backend.main
       │    ├─ backend.jsonl
       │    ├─ uvicorn.jsonl
       │    └─ browser.jsonl
       ├─ starts Vite
       │    └─ vite.jsonl
       ├─ mirrors child output to the terminal
       └─ coordinates rotation cleanup and child shutdown

React browser logger
  └─ batched POST /api/logs/browser
       └─ FastAPI validation, sanitization, and browser logger
```

### Component boundaries

#### `backend/logging_config.py`

Owns logging configuration and filesystem policy:

- Parse and validate logging environment variables.
- Create or join a run context supplied through environment variables.
- Build human-readable console handlers and JSONL file handlers.
- Route backend, Uvicorn, and browser loggers to distinct files without duplication.
- Format the common JSONL event envelope.
- Redact sensitive keys and API-key-shaped values recursively.
- Rotate files by size.
- Run retention cleanup under an inter-process filesystem lock.
- Create and atomically update `logs/latest`.

The module exposes narrow setup and policy functions so formatting, redaction, rotation, and retention can be tested without starting the application.

#### `backend/dev_runner.py`

Owns the local process lifecycle:

- Resolve the repository root independent of the caller's current directory.
- Create the UTC run ID and run directory.
- Write an initial manifest with effective non-secret settings.
- Start the backend using the active Python interpreter.
- Start Vite with its working directory set to `frontend/`.
- Pass the run ID and run directory to the backend process.
- Mirror Vite stdout and stderr to the terminal while wrapping each line as a structured Vite file event.
- Preserve backend/Uvicorn console output in the shared terminal.
- Forward `SIGINT` and `SIGTERM` to both children.
- Stop the sibling process if either child exits unexpectedly.
- Atomically finalize the manifest with exit codes and clean or unclean shutdown state.

The supervisor is the only writer of `manifest.json`. Backend logging handlers are the only writers of the three backend-owned JSONL files, and the supervisor is the only writer of `vite.jsonl`; this avoids unsafe multi-process writes to one file.

#### `backend/main.py`

Owns request context and browser ingestion:

- Initialize logging before serving requests.
- Add middleware that accepts a valid incoming `X-Request-ID` or creates a UUID, binds it to a `contextvars.ContextVar`, returns it in the response, and logs method, path, status, and duration.
- Bind the conversation ID while processing conversation routes.
- Add `POST /api/logs/browser` for validated browser batches.
- Route browser events to the dedicated `browser` logger.

Async tasks inherit the request context, allowing title generation, council stages, and parallel model calls to share correlation fields.

#### `backend/openrouter.py` and `backend/council.py`

Replace `print` and add structured domain events:

- Model request started, completed, timed out, or failed.
- HTTP status and sanitized error category.
- Model identifier and duration.
- Council stage started and completed, including response counts and duration.
- Chairman fallback and all-models-failed events.
- Prompts and model responses only when `LOG_LLM_PAYLOADS=true`.

No authorization header or API key is passed to a logging call. Payload events still pass through recursive redaction and event-size limits.

#### `frontend/src/logger.js`

Owns browser capture and transport:

- Generate one browser session ID per page load.
- Preserve and wrap `console.warn` and `console.error`.
- Capture `window.error` and `unhandledrejection`.
- Provide an explicit application-event API for future instrumentation.
- Normalize errors without serializing arbitrary cyclic objects.
- Redact known sensitive fields before transport.
- Limit individual event size and queue length.
- Batch by count and timer, with a final best-effort page-unload flush.
- Use a recursion guard so logging transport failures never create new log events.
- Drop the oldest queued events when the bounded queue is full.

At the default `WARNING` browser level, ordinary `console.log` and `console.info` calls remain local. Setting the browser level to `DEBUG` enables their forwarding.

#### `start.sh`

Resolve its own repository directory and replace itself with:

```bash
uv run python -m backend.dev_runner
```

The supervisor, rather than shell background jobs, owns process cleanup and exit status.

## Run Layout and Discovery

Each run uses a filesystem-safe UTC identifier without colons. The normal run
ID is `YYYY-MM-DDTHHMMSSZ`; if that directory already exists, the allocated run
ID and directory name become `YYYY-MM-DDTHHMMSSZ-1`, then `-2`, and so on:

```text
logs/
  latest -> runs/2026-08-26T220712Z/
  runs/
    2026-08-26T220712Z/
      backend.jsonl
      backend.jsonl.1
      uvicorn.jsonl
      vite.jsonl
      browser.jsonl
      manifest.json
```

`logs/latest` is replaced atomically after the run directory exists. `logs/` is ignored by Git. Agents can start with `logs/latest/manifest.json` and then search the relevant JSONL source by request, conversation, model, level, or event name.

The manifest contains:

- Run ID and UTC start/end timestamps.
- Repository root and non-secret effective logging configuration.
- Backend and frontend commands, ports, and process IDs.
- Child exit codes.
- Clean or unclean shutdown status.
- Retention actions performed during startup and shutdown.

The manifest never contains environment-variable values classified as secret.

## Event Format

File output is one valid JSON object per line. Every event has this envelope:

```json
{
  "timestamp": "2026-08-26T22:07:12.438Z",
  "level": "INFO",
  "source": "backend",
  "logger": "llm_council.openrouter",
  "event": "openrouter.request.completed",
  "message": "Model request completed",
  "run_id": "2026-08-26T220712Z",
  "request_id": "a81c33a5-0fe9-4a53-b6aa-47db0e98c711"
}
```

Optional fields include `conversation_id`, `browser_session_id`, `client_timestamp`, `model`, `stage`, `duration_ms`, `status`, `http_status`, `error_type`, `stream`, and sanitized `details`.

Server timestamps are authoritative UTC RFC 3339 timestamps. Browser events retain a separately named client timestamp. Exceptions are represented by type, message, and stack text after sanitization; raw exception objects are never passed to JSON serialization.

Terminal output is concise text rather than JSON:

```text
22:07:12 INFO  [backend] [req:a81c33a5] Stage 1 started
22:07:13 ERROR [openrouter] [req:a81c33a5] model request failed status=402
22:07:13 WARN  [browser] Unhandled promise rejection
```

ANSI color is allowed only on interactive terminal handlers. Files never contain ANSI escape codes.

## Correlation

- Every HTTP request receives an `X-Request-ID` response header.
- Conversation routes bind their path conversation ID to the logging context.
- Council stages and OpenRouter requests inherit both values through context variables.
- Browser events include a browser session ID and may include a known conversation ID.
- A browser ingestion HTTP request has its own server request ID; the original browser event fields remain nested and cannot override trusted server fields.

Client-supplied identifiers are length-limited and treated as data. They cannot overwrite `source`, server `timestamp`, trusted `request_id`, or effective severity.

## Configuration

The ignored `.env` contains documented placeholders and defaults:

```dotenv
# General logging
LOG_LEVEL=INFO
LOG_DIR=logs

# Per-source overrides
LOG_BACKEND_LEVEL=INFO
LOG_UVICORN_LEVEL=INFO
LOG_VITE_LEVEL=INFO
LOG_BROWSER_LEVEL=WARNING

# Rotation and retention
LOG_MAX_BYTES=10485760
LOG_RETENTION_DAYS=14
LOG_TOTAL_MAX_BYTES=524288000

# Sensitive LLM content; leave disabled for normal development
LOG_LLM_PAYLOADS=false

# Browser batching and safety limits
LOG_BROWSER_BATCH_SIZE=20
LOG_BROWSER_FLUSH_MS=2000
LOG_BROWSER_QUEUE_LIMIT=200
LOG_EVENT_MAX_BYTES=65536
```

Invalid values produce one terminal warning and fall back to the documented default. Effective non-secret values are recorded in the manifest. Frontend build-time values required by `logger.js` use matching `VITE_`-prefixed variables derived explicitly by the supervisor rather than exposing the full process environment.

## Redaction and Content Policy

LLM metadata is always eligible for logging: model, stage, duration, status, response size, and sanitized failure information. Prompts, user content, peer responses, ranking text, and chairman output are absent by default.

`LOG_LLM_PAYLOADS=true` enables those payload fields for local debugging. The following remain redacted in every mode:

- Authorization and proxy-authorization headers.
- API keys, bearer tokens, cookies, and set-cookie values.
- Keys named `password`, `secret`, `token`, `api_key`, `authorization`, or close case-insensitive variants.
- Values matching supported API-key and bearer-token patterns.

Oversized strings are truncated with original byte length and truncation status recorded. Redaction occurs before formatting, file writing, or browser transport.

## Browser Ingestion Contract

`POST /api/logs/browser` accepts a JSON batch with at most `LOG_BROWSER_BATCH_SIZE` events and rejects a body or event exceeding configured limits. Each event contains a client timestamp, requested level, event name, message, browser session ID, page location without credentials, and optional sanitized details.

The endpoint:

- Accepts only the configured local frontend origins.
- Uses Pydantic models with bounded field lengths and forbids unexpected top-level control fields.
- Maps client levels into a fixed allowed set.
- Applies server-side redaction again.
- Replaces trusted timestamp/source/request fields with server values.
- Returns success after events are synchronously accepted by the local logging handler.

Invalid browser log batches return a normal 4xx response but never affect other application endpoints. The browser transport does not retry indefinitely; it preserves a bounded queue and silently backs off after failure.

## Rotation and Retention

Each source rotates independently when its active file would exceed 10 MiB. Rotated segments remain in the same run directory with numeric suffixes. Rotation and cleanup use a lock file under `logs/` to prevent the backend process and supervisor from modifying the log tree concurrently.

Cleanup runs:

1. Before a new run becomes active.
2. After a successful file rotation.
3. During supervisor shutdown.

The algorithm is deterministic:

1. Delete completed run directories older than 14 days, oldest first.
2. Recalculate total bytes under `logs/runs`.
3. If total size exceeds 500 MiB, delete oldest completed run directories until under the cap.
4. If the active run alone still exceeds the cap, delete its oldest rotated segments across sources until under the cap.
5. Never delete an active base file, the active manifest, the `latest` link target, or the retention lock.

If active base files alone exceed the cap, cleanup records that the hard floor prevents compliance and leaves them intact. Cleanup actions are returned to the caller. The supervisor records startup and shutdown actions in the manifest; a backend handler records post-rotation actions in the backend log after releasing the cleanup lock. The cleanup routine does not recursively log while holding its lock.

## Failure Handling

- If the log directory or file handlers cannot initialize, the application continues with terminal-only logging and prints a prominent warning.
- If retention or manifest updates fail, the supervisor reports the failure but still starts or stops the application normally.
- If either child exits unexpectedly, the supervisor gracefully stops the sibling and exits nonzero.
- `SIGINT` and `SIGTERM` are forwarded to both children. After a bounded grace period, remaining children are terminated and then killed only if required.
- Browser capture and transport failures never throw into application code or recursively log themselves.
- Malformed objects, cyclic browser values, Unicode errors, and JSON formatting failures degrade to a bounded safe representation.
- One source's file-handler failure does not disable console output or other source files.

## Testing Strategy

Python tests use `unittest`, `tempfile`, `unittest.mock`, and FastAPI's existing test client. They make no live OpenRouter requests.

### Backend unit and integration tests

- JSONL schema, UTC timestamp formatting, and ANSI-free files.
- Human terminal formatting and level filtering.
- Recursive redaction in metadata and payload-enabled modes.
- String truncation and event-size enforcement.
- Rotation at a small test threshold.
- Age cleanup, total-size cleanup, active-run protection, and deterministic deletion order.
- Inter-process cleanup locking and atomic `latest` replacement.
- Request-ID generation, accepted incoming IDs, response headers, and context propagation.
- Browser endpoint origin, schema, level, batch-size, event-size, and trusted-field validation.
- Supervisor child startup, unexpected exit, signal forwarding, grace timeout, and manifest finalization using mocked subprocesses.
- OpenRouter and council success/failure/timing events using mocked HTTP calls.
- Payload absence by default and sanitized payload presence when explicitly enabled.

### Frontend tests

Use Node's built-in test runner so the feature adds no frontend test framework dependency:

- Error and unhandled-rejection normalization.
- Preservation of original console methods.
- Level filtering, count batching, timer flushing, and unload flushing.
- Bounded queue behavior and oldest-event dropping.
- Redaction, cyclic-object handling, and size truncation.
- Transport-failure recursion protection.

### End-to-end smoke test

A local smoke test uses temporary log paths and reduced rotation thresholds. It verifies:

1. `start.sh` shows backend, Uvicorn, Vite, and forwarded browser events in its terminal.
2. All four JSONL files and the manifest appear through `logs/latest`.
3. A mocked council request shares correlation fields across relevant events.
4. Rotation and retention occur under reduced thresholds.
5. A marker-secret scan finds no authorization, cookie, API-key, or configured secret value.
6. `Ctrl+C` stops both child processes and records a clean shutdown.

The repository currently has four baseline ESLint errors. Implementation includes only the minimal fixes needed to restore a clean `npm run lint` result: effect-local async loading functions in `App.jsx` and removal of unused React imports in `Sidebar.jsx`. No unrelated frontend refactor is included.

## Acceptance Criteria

- `./start.sh` is the only command required to start both services with logging enabled.
- A person sees concise logs from every source in the launching terminal.
- An agent can open `logs/latest/manifest.json` and source-specific JSONL files without access to that terminal.
- Files are valid JSONL, separately routed, correlated, redacted, rotated, and retained according to the agreed limits.
- Browser logging is bounded, origin-validated, and unable to break the UI.
- Payload logging is disabled by default and explicitly configurable.
- Shutdown state and child exit codes are recorded accurately.
- Python compilation, backend tests, frontend tests, `npm run lint`, and `npm run build` pass.
- No live OpenRouter credit is required by automated tests.
- Generated logs remain ignored by Git.

## Rollback

Rollback restores the original `start.sh`, removes the supervisor and logging modules, removes browser logger initialization and ingestion endpoint, removes the logging environment settings, and removes the new tests. `logs/` is disposable runtime data and remains safe to delete after the application is stopped.
