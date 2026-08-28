# Task 3 Report: Expire Abandoned Running Manifests

## Implementation

Root cause:
- `backend/logging_config.py` treated any manifest with `status` of `starting` or `running` and no `ended_at` as active, regardless of whether any recorded owning process still existed.
- `backend/dev_runner.py` did not record the supervisor PID in the initial manifest, so retention could not distinguish a live run from an abandoned legacy manifest unless a child PID happened to be present later.

Changes made:
- Added `supervisor_pid` to the initial startup manifest before any child process launch in `backend/dev_runner.py`.
- Tightened `_active_run_identities()` in `backend/logging_config.py` so `starting`/`running` manifests are protected only when at least one recorded positive owner PID is alive.
- Added `_manifest_has_live_owner()`, `_is_positive_pid()`, and `_pid_is_alive()` helpers.
- Implemented PID liveness rules exactly as requested:
  - positive integer PIDs only
  - `ProcessLookupError` means dead
  - `PermissionError` means alive
  - malformed or missing PID data is ignored
- Preserved the existing unconditional protection for the explicitly passed `current_run_dir` in `cleanup_logs()`.

## Files Changed

- `backend/dev_runner.py`
- `backend/logging_config.py`
- `tests/test_dev_runner.py`
- `tests/test_log_retention.py`

## RED / GREEN

Focused RED commands:

```bash
env UV_CACHE_DIR=/private/tmp/llm-council-uv-cache uv run python -m unittest tests.test_log_retention.RetentionTests.test_deletes_expired_running_manifest_without_a_live_recorded_owner -v
```

Relevant output:

```text
FAIL: test_deletes_expired_running_manifest_without_a_live_recorded_owner
AssertionError: True is not false
```

```bash
env UV_CACHE_DIR=/private/tmp/llm-council-uv-cache uv run python -m unittest tests.test_dev_runner.SupervisorTests.test_records_supervisor_pid_in_starting_manifest_before_children_launch -v
```

Relevant output:

```text
ERROR: test_records_supervisor_pid_in_starting_manifest_before_children_launch
KeyError: 'supervisor_pid'
```

Characterization check run during RED phase:

```bash
env UV_CACHE_DIR=/private/tmp/llm-council-uv-cache uv run python -m unittest tests.test_log_retention.RetentionTests.test_preserves_another_run_that_is_still_running_with_a_live_recorded_owner -v
```

Relevant output:

```text
OK
```

Focused GREEN commands:

```bash
env UV_CACHE_DIR=/private/tmp/llm-council-uv-cache uv run python -m unittest tests.test_log_retention.RetentionTests.test_deletes_expired_running_manifest_without_a_live_recorded_owner -v
env UV_CACHE_DIR=/private/tmp/llm-council-uv-cache uv run python -m unittest tests.test_log_retention.RetentionTests.test_preserves_another_run_that_is_still_running_with_a_live_recorded_owner -v
env UV_CACHE_DIR=/private/tmp/llm-council-uv-cache uv run python -m unittest tests.test_dev_runner.SupervisorTests.test_records_supervisor_pid_in_starting_manifest_before_children_launch -v
```

Relevant output:

```text
OK
OK
OK
```

## Broad Verification

Required full suite:

```bash
env UV_CACHE_DIR=/private/tmp/llm-council-uv-cache uv run python -m unittest discover -s tests -v
```

Relevant output:

```text
Ran 70 tests in 0.531s
OK (skipped=1)
```

Compile check:

```bash
env UV_CACHE_DIR=/private/tmp/llm-council-uv-cache uv run python -m compileall backend tests
```

Relevant output:

```text
Listing 'backend'...
Listing 'tests'...
```

## Self-Review

- Diff stays within the requested blast radius: one new manifest field, one retention decision point, and targeted regression coverage.
- The retention change is root-cause aligned: abandoned active manifests now age out only when no live recorded owner remains, while the current run argument remains protected separately.
- Tests exercise real retention behavior and real manifest serialization; mocking is limited to `os.kill(..., 0)` liveness outcomes.
- No secrets are introduced into manifests or test output.

## Concerns

- The live-owner retention test is a characterization guard more than a red-to-green test because prior behavior already preserved every unended running manifest. The actual behavior change was driven by the abandoned-run deletion test and the supervisor-manifest serialization test.
