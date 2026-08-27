# Agent-Friendly Logging Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five load-bearing findings from the final logging re-review without changing the approved logging architecture or merging the feature branch.

**Architecture:** Preserve the existing browser logger, FastAPI ingestion endpoint, JSONL formatter, and retention cleanup boundaries. Harden each boundary in place: redact complete browser header values, enforce a shared serialized batch-byte contract, make every JSONL payload respect its configured limit, and make malformed URL/manifest inputs degrade safely.

**Tech Stack:** JavaScript ES modules with Node's built-in test runner, React/Vite, Python 3.10+, FastAPI/Pydantic, Python standard-library logging and `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-26-agent-friendly-logging-design.md`

## Global Constraints

- Work only on `codex/agent-friendly-logging-impl` in the existing `.worktrees/agent-friendly-logging-impl` worktree.
- Do not merge, push, amend, force-push, add dependencies, or change `.env` values.
- Preserve terminal output, the four source-specific JSONL files, rotation, retention, request correlation, and browser best-effort behavior.
- Redaction must happen before browser transport and again at backend ingestion/formatting.
- `LOG_EVENT_MAX_BYTES` bounds each compact JSON event and each browser transport body, measured as UTF-8 bytes without an HTTP framing allowance.
- Every non-empty JSONL record must remain valid JSON. The smallest supported configured event limit is 2 bytes, which can represent `{}`.
- Keep implementation diffs local to the files named by each task.
- Use TDD: demonstrate each new regression failing before implementation, then pass the focused tests before committing.

## Execution Routing

| Task | Implementer | Scoped reviewer | Reason |
| --- | --- | --- | --- |
| 1. Browser header-value redaction | `gpt-5.6-luna`, medium | `gpt-5.6-terra`, medium | Narrow implementation with a security-sensitive review. |
| 2. Browser batch byte contract | `gpt-5.6-terra`, medium | `gpt-5.6-terra`, high | Crosses event construction and transport batching. |
| 3. JSONL small-limit enforcement | `gpt-5.6-luna`, medium | `gpt-5.6-terra`, medium | Bounded formatter fallback with exact tests. |
| 4. Malformed active manifests | `gpt-5.6-luna`, medium | `gpt-5.6-luna`, medium | Small defensive type guard. |
| 5. Malformed browser pages | `gpt-5.6-luna`, medium | `gpt-5.6-luna`, medium | Small defensive URL sanitizer. |
| Final whole-branch review | — | `gpt-5.6-sol`, high | One independent final gate; do not use `xhigh` by default. |

Escalate an implementation task one tier only after a concrete failed attempt or an unresolved scoped-review finding. Keep agent reports concise and point reviewers to the task diff and focused test output.

---

### Task 1: Redact Complete Header-Style Values Before Browser Transport

**Files:**
- Modify: `frontend/src/logger.js:3-98`
- Test: `frontend/src/logger.test.js:208-230`

**Interfaces:**
- Consumes: `sanitizeBrowserValue(value, { eventMaxBytes, seen })` and the existing recursive sensitive-key redaction.
- Produces: a private `redactBrowserString(value)` helper that removes token-shaped values and replaces complete inline `Cookie:`, `Set-Cookie:`, `Authorization:`, and `Proxy-Authorization:` values with `[REDACTED]` before truncation or transport.

- [ ] **Step 1: Add failing browser redaction regressions**

Extend the existing sensitive-key test and add a string-value test that inspects the captured transport body:

```js
test('redacts complete header-style values before browser transport', async () => {
  const sent = [];
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 65536,
    windowObject: createFakeWindow(),
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });

  logger.log(
    'ERROR',
    'headers.exposed',
    'Cookie: session=one; csrf=two',
    {
      response: 'Set-Cookie: sid=three; theme=four',
      auth: 'Authorization: Basic basic-secret',
      proxy: 'Proxy-Authorization: Bearer proxy-secret-value',
      safe: 'visible',
    },
  );
  await logger.flush();

  const serialized = JSON.stringify(sent);
  assert.doesNotMatch(serialized, /session=one|csrf=two|sid=three|theme=four/);
  assert.doesNotMatch(serialized, /basic-secret|proxy-secret-value/);
  assert.match(serialized, /Cookie: \[REDACTED\]/);
  assert.match(serialized, /Set-Cookie: \[REDACTED\]/);
  assert.match(serialized, /"safe":"visible"/);
});
```

Also include mixed-case and whitespace/hyphen variants so the client matches the backend policy.

- [ ] **Step 2: Run the focused frontend test and verify RED**

Run:

```bash
cd frontend
npm test -- --test-name-pattern='header-style values'
```

Expected: FAIL because the current string sanitizer redacts token patterns but leaves cookie pairs after `Cookie:` and `Set-Cookie:`.

- [ ] **Step 3: Implement complete inline-header redaction**

In `frontend/src/logger.js`, define explicit string patterns next to `TOKEN_VALUE` and route every string through a single helper before `truncateBrowserText`:

```js
const HEADER_VALUE = /(?<prefix>\b(?:proxy[\s-]*authorization|set[\s-]*cookie|authorization|cookie)\s*:\s*)[^\r\n]*/gi;

function redactBrowserString(value) {
  return String(value)
    .replace(HEADER_VALUE, (...args) => {
      const groups = args.at(-1);
      return `${groups.prefix}[REDACTED]`;
    })
    .replace(TOKEN_VALUE, (match) => `${match.slice(0, 8)}[REDACTED]`);
}
```

Use a callback form that remains correct in the repository's supported Node/browser runtime. Preserve newlines so one header does not consume unrelated following text. Call `truncateBrowserText(redactBrowserString(value), eventMaxBytes)` from the string branch of `sanitizeBrowserValue`.

- [ ] **Step 4: Run focused and full frontend checks**

Run:

```bash
cd frontend
npm test
npm run lint
```

Expected: all logger tests and ESLint pass; serialized test transport bodies contain none of the header secrets.

- [ ] **Step 5: Commit Task 1**

```bash
git add frontend/src/logger.js frontend/src/logger.test.js
git commit -m "bugfix: redact browser header values"
```

---

### Task 2: Enforce the Browser Transport Batch Byte Contract

**Files:**
- Modify: `frontend/src/logger.js:22-68,178-224`
- Test: `frontend/src/logger.test.js:160-206,321-360`
- Test: `tests/test_http_logging.py:148-168`

**Interfaces:**
- Consumes: `serializedEventBytes(event)`, `eventMaxBytes`, `batchSize`, the queue, and backend `BrowserLogBatch.model_dump_json()` validation.
- Produces: `serializedBatchBytes(events) -> number` and a private `takeTransportBatch()` that removes at most `batchSize` queued events while guaranteeing `JSON.stringify({ events })` is at most `eventMaxBytes` UTF-8 bytes.

- [ ] **Step 1: Add failing frontend batch-envelope tests**

Add a test using several individually valid events whose wrapper would exceed the configured limit:

```js
test('keeps every transmitted batch within the backend byte limit', async () => {
  const sent = [];
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'DEBUG',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 256,
    windowObject: createFakeWindow(),
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });

  logger.log('ERROR', 'first.event', 'x'.repeat(80));
  logger.log('ERROR', 'second.event', 'y'.repeat(80));
  logger.log('ERROR', 'third.event', 'z'.repeat(80));
  await logger.flush();

  assert.ok(sent.length >= 2);
  for (const body of sent) {
    assert.ok(new TextEncoder().encode(JSON.stringify(body)).byteLength <= 256);
  }
  assert.equal(sent.flatMap((body) => body.events).length, 3);
});
```

Add the same assertion for the `pagehide` beacon body. Change existing per-event assertions to assert the wrapped `{ events: [...] }` size as well.

- [ ] **Step 2: Add an endpoint compatibility regression**

In `tests/test_http_logging.py`, post a schema-valid event close to the byte limit under a patched `LOG_EVENT_MAX_BYTES`, then assert the exact wrapped payload at or below the limit returns 202 while one byte above returns 413:

```python
def test_browser_batch_byte_limit_matches_complete_serialized_body(self):
    event = {
        "client_timestamp": "2026-08-26T22:07:12.438Z",
        "level": "ERROR",
        "event": "browser.error",
        "message": "bounded",
        "browser_session_id": "session-1",
        "page": "http://localhost:5173/",
    }
    exact_size = len(
        BrowserLogBatch.model_validate({"events": [event]}).model_dump_json().encode("utf-8")
    )
    with patch.dict(os.environ, {"LOG_EVENT_MAX_BYTES": str(exact_size)}):
        accepted = self.client.post(
            "/api/logs/browser",
            headers={"Origin": "http://localhost:5173"},
            json={"events": [event]},
        )
    with patch.dict(os.environ, {"LOG_EVENT_MAX_BYTES": str(exact_size - 1)}):
        rejected = self.client.post(
            "/api/logs/browser",
            headers={"Origin": "http://localhost:5173"},
            json={"events": [event]},
        )
    self.assertEqual(accepted.status_code, 202)
    self.assertEqual(rejected.status_code, 413)
```

Import `BrowserLogBatch` from `backend.main` for the test so the expectation uses the server's canonical serialization.

- [ ] **Step 3: Run both regressions and verify RED**

Run:

```bash
cd frontend
npm test -- --test-name-pattern='transmitted batch|pagehide'
cd ..
uv run python -m unittest tests.test_http_logging.HttpLoggingTests.test_browser_batch_byte_limit_matches_complete_serialized_body -v
```

Expected: the frontend test fails because current `flush()` takes `batchSize` events without measuring the wrapper; the endpoint boundary test documents the server's existing complete-body behavior.

- [ ] **Step 4: Implement greedy byte-bounded batching**

Add helpers that use the exact body shape sent to the backend:

```js
function serializedBatchBytes(events) {
  return new TextEncoder().encode(JSON.stringify({ events })).byteLength;
}

function takeTransportBatch(queue, batchSize, eventMaxBytes) {
  const events = [];
  while (events.length < batchSize && queue.length > 0) {
    const candidate = [...events, queue[0]];
    if (serializedBatchBytes(candidate) <= eventMaxBytes) {
      events.push(queue.shift());
    } else if (events.length === 0) {
      queue.shift();
    } else {
      break;
    }
  }
  return events;
}
```

Build each event against the single-event transport budget, not the bare-event budget:

```js
function wrappedEventBytes(event) {
  return serializedBatchBytes([event]);
}
```

Use `wrappedEventBytes` inside `truncateBrowserEvent` and `compactEventField`. Use `takeTransportBatch` from both `flush()` and `onPageHide()`. If an event cannot fit even alone, drop it once; never loop forever, retry, throw into application code, or emit an empty batch. Preserve order and queue bounds.

- [ ] **Step 5: Run focused compatibility checks**

Run:

```bash
cd frontend
npm test
npm run lint
cd ..
uv run python -m unittest tests.test_http_logging -v
```

Expected: all frontend logger and HTTP logging tests pass. Every fetch/beacon body is within `eventMaxBytes`, all queued events that can fit are eventually sent in order, and the endpoint accepts the boundary payload.

- [ ] **Step 6: Commit Task 2**

```bash
git add frontend/src/logger.js frontend/src/logger.test.js tests/test_http_logging.py
git commit -m "bugfix: bound browser log batches"
```

---

### Task 3: Enforce JSONL Aggregate Limits Below 64 Bytes

**Files:**
- Modify: `backend/logging_config.py:164-209,499-542,671-734`
- Test: `tests/test_logging_config.py:14-46,130-178`

**Interfaces:**
- Consumes: `LoggingSettings.from_env`, `JsonLineFormatter`, `_serialize_payload`, and `_fit_payload`.
- Produces: a supported minimum `LOG_EVENT_MAX_BYTES` of 2, unconditional aggregate fitting, and a `_fit_payload` result whose compact UTF-8 serialization never exceeds the validated limit.

- [ ] **Step 1: Add failing small-limit regressions**

Extend `LoggingSettingsTests` and the formatter-size test:

```python
def test_event_limit_below_minimum_falls_back_safely(self):
    warnings = []
    settings = LoggingSettings.from_env(
        {"LOG_EVENT_MAX_BYTES": "1"}, warning_sink=warnings.append
    )
    self.assertEqual(settings.event_max_bytes, 65536)
    self.assertEqual(len(warnings), 1)
    self.assertNotIn("1", warnings[0])

def test_json_formatter_caps_complete_payload_at_small_supported_limits(self):
    for max_bytes in (2, 8, 16, 32, 63):
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
        json.loads(serialized)
```

Also assert `JsonLineFormatter(event_max_bytes=1)` raises `ValueError`, because no valid JSON document can fit in one byte.

- [ ] **Step 2: Run the focused formatter tests and verify RED**

Run:

```bash
uv run python -m unittest \
  tests.test_logging_config.LoggingSettingsTests.test_event_limit_below_minimum_falls_back_safely \
  tests.test_logging_config.RedactionTests.test_json_formatter_caps_complete_payload_at_small_supported_limits -v
```

Expected: FAIL because limits below 64 currently bypass `_fit_payload`.

- [ ] **Step 3: Validate the minimum and fit every payload**

Generalize the integer parser with a keyword-only minimum:

```python
def _parse_positive_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    warning_sink: Callable[[str], None],
    *,
    minimum: int = 1,
) -> int:
    # Parse as today; warn and return default when parsed < minimum.
```

Call it with `minimum=2` only for `LOG_EVENT_MAX_BYTES`. Validate the same invariant in `JsonLineFormatter.__init__`:

```python
if event_max_bytes < 2:
    raise ValueError("event_max_bytes must be at least 2")
```

Remove the `>= 64` bypass and always call `_fit_payload`. After the existing envelope/optional-field attempts, return `{}` when necessary; `{}` is the valid two-byte hard floor:

```python
empty_payload: dict[str, Any] = {}
if len(_serialize_payload(empty_payload).encode("utf-8")) <= max_bytes:
    return empty_payload
raise ValueError("max_bytes cannot contain a JSON object")
```

Do not return `{"message": ""}` unless its actual compact serialization fits.

- [ ] **Step 4: Run focused and complete backend tests**

Run:

```bash
uv run python -m unittest tests.test_logging_config -v
uv run python -m unittest discover -s tests -v
```

Expected: all tests pass; every formatter output is valid JSON and within the configured supported byte limit.

- [ ] **Step 5: Commit Task 3**

```bash
git add backend/logging_config.py tests/test_logging_config.py
git commit -m "bugfix: enforce small JSON log limits"
```

---

### Task 4: Tolerate Non-Object Active-Run Manifests

**Files:**
- Modify: `backend/logging_config.py:433-447`
- Test: `tests/test_log_retention.py:299-319`

**Interfaces:**
- Consumes: `_active_run_identities(runs_dir)` and `cleanup_logs(settings, current_run_dir, now)`.
- Produces: active-manifest discovery that calls mapping methods only after `isinstance(manifest, Mapping)` and treats valid non-object JSON as an unreadable/non-active manifest without aborting cleanup.

- [ ] **Step 1: Add failing malformed-manifest regressions**

Add a table-driven retention test for valid JSON values that are not objects:

```python
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
```

This establishes the policy: non-object JSON is not trusted as an active lease, but it must not abort startup cleanup.

- [ ] **Step 2: Run the retention regression and verify RED**

Run:

```bash
uv run python -m unittest tests.test_log_retention.RetentionTests.test_cleanup_tolerates_non_object_active_manifests -v
```

Expected: ERROR with `AttributeError` when `_active_run_identities` calls `.get()` on a list or scalar.

- [ ] **Step 3: Add the mapping type guard**

Immediately after `json.load(handle)`, keep the existing exception behavior and skip non-mappings:

```python
if not isinstance(manifest, Mapping):
    continue
if manifest.get("status") in {"starting", "running"} and not manifest.get("ended_at"):
    active.add(_path_identity(run_dir))
```

Do not broaden the exception handler to hide unrelated programmer errors. Preserve active object manifests and all symlink protections.

- [ ] **Step 4: Run focused and complete retention tests**

Run:

```bash
uv run python -m unittest tests.test_log_retention -v
uv run python -m unittest discover -s tests -v
```

Expected: all tests pass; malformed valid JSON does not abort cleanup, and genuinely running object manifests remain protected.

- [ ] **Step 5: Commit Task 4**

```bash
git add backend/logging_config.py tests/test_log_retention.py
git commit -m "bugfix: tolerate malformed log manifests"
```

---

### Task 5: Degrade Safely on Malformed Browser Page URLs

**Files:**
- Modify: `backend/main.py:175-199,419-423`
- Test: `tests/test_http_logging.py:102-168`

**Interfaces:**
- Consumes: schema-valid `BrowserLogEvent.page` strings and `_sanitize_browser_page(value)`.
- Produces: `_sanitize_browser_page(value) -> str` that never raises for a string accepted by the Pydantic schema, removes credentials/query/fragment for parseable URLs, and returns a bounded safe fallback for malformed inputs.

- [ ] **Step 1: Add failing malformed-URL endpoint regressions**

Add an endpoint test using an unmatched IPv6 bracket, which is schema-valid text but makes `urllib.parse.urlsplit()` raise `ValueError`:

```python
def test_browser_ingestion_degrades_safely_for_malformed_pages(self):
    response = self.client.post(
        "/api/logs/browser",
        headers={"Origin": "http://localhost:5173"},
        json={
            "events": [{
                "client_timestamp": "2026-08-26T22:07:12.438Z",
                "level": "ERROR",
                "event": "browser.malformed_page",
                "message": "boom",
                "browser_session_id": "session-1",
                "page": "https://user:password@[::1?token=secret#fragment",
            }],
        },
    )
    self.assertEqual(response.status_code, 202)
```

Capture the browser logger as in `test_browser_batch_is_logged_with_server_owned_source` and assert the stored page contains no `token=secret` or fragment text.

- [ ] **Step 2: Run the endpoint regression and verify RED**

Run:

```bash
uv run python -m unittest tests.test_http_logging.HttpLoggingTests.test_browser_ingestion_degrades_safely_for_malformed_pages -v
```

Expected: FAIL/ERROR because a schema-valid malformed page can raise `ValueError` during `_sanitize_browser_page` and return HTTP 500.

- [ ] **Step 3: Make page sanitization total and bounded**

Contain URL parsing in the sanitizer rather than around the endpoint loop:

```python
def _sanitize_browser_page(value: str) -> str:
    """Return a credential/query/fragment-free page without raising."""
    raw = str(value)
    try:
        parts = urlsplit(raw)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        sanitized = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except (TypeError, ValueError):
        sanitized = raw.split("?", 1)[0].split("#", 1)[0]
        if "@" in sanitized:
            sanitized = sanitized.rsplit("@", 1)[-1]
    return sanitized[:2048] or "/"
```

The fallback must not echo userinfo, query parameters, or fragments and must never include raw exception text.

- [ ] **Step 4: Run endpoint, lint, and full verification**

Run:

```bash
uv run python -m unittest tests.test_http_logging -v
uv run python -m unittest discover -s tests -v
cd frontend
npm test
npm run lint
npm run build
```

Expected: Python tests, frontend tests, lint, and production build all pass.

- [ ] **Step 5: Run the opt-in real-process logging smoke test**

Run:

```bash
RUN_LOGGING_SMOKE=1 uv run python -m unittest tests.test_logging_smoke -v
```

Expected: the supervisor starts both services, all four source files and the manifest parse, shutdown is clean, and no secret marker is persisted. The smoke test must not call OpenRouter.

- [ ] **Step 6: Commit Task 5**

```bash
git add backend/main.py tests/test_http_logging.py
git commit -m "bugfix: tolerate malformed browser pages"
```

---

## Final Review and Branch State

- [ ] Dispatch one fresh `gpt-5.6-sol` reviewer at `high` effort against the approved spec, this remediation plan, and the complete diff from `92e1fcc` to branch HEAD.
- [ ] Require the reviewer to classify findings as Critical, Important, or Minor and explicitly re-check all five residual findings.
- [ ] If Critical or Important findings remain, record them and stop at the gate; do not merge or push.
- [ ] If the review is clean, run `git status -sb`, record exact test totals and the final commit list, and report that the branch is ready for the user's integration decision.
- [ ] Do not invoke the branch-finishing workflow, merge, push, or delete the worktree until the user explicitly asks.
