# Testing Guide

This document describes the testing strategies and workflows used in provide-terminal.

## Running Tests

### Quick Start

Run the standard test suite (excludes slow/memory tests):

```bash
uv run pytest
```

> **Note:** The root suite includes Cloudflare Worker tests. The CF vendor tree (`packages/provide-terminal-cloudflare/python_modules/`) is `.gitignore`d and not present on a clean checkout — the vendor guard test skips automatically in that case. To run the full CF suite including the vendor guard, initialise the CF working directory first (`pywrangler sync` from `packages/provide-terminal-cloudflare/`).

Or use the pytest gate script (recommended for local development):

```bash
uv run python scripts/run_pytest_gate.py
```

### Test Markers

Tests are organized by markers, which allow selective execution:

#### Default Tests (run by default)

- **Standard unit and integration tests** - Cover core functionality with 100% branch coverage requirement
  - Run with: `uv run pytest` (default)

#### Deselected Tests (opt-in)

- **`playwright`** - Browser-based UI tests

  - Run with: `uv run pytest -m playwright`
  - Files: `tests/playwright/`
  - Time: ~5-10 minutes
  - Note: Requires `playwright install chromium` first

- **`mutant`** - Mutation testing and mutmut-focused tests

  - Run with: `uv run pytest -m mutant` or `uv run python scripts/run_mutation_gate.py --min-mutation-score 100`
  - Files: `tests_mutation/`, mutation-specific test files
  - Time: 5-15 minutes per test
  - Coverage: Validates test suite quality by checking test failure on code mutations

- **`memray`** - Memory profiling and allocation stress tests

  - Run with: `uv run pytest tests/memray/ -m memray -v --no-cov`
  - Files: `tests/memray/`
  - Time: ~15-30 minutes total
  - Coverage: Monitors memory allocations across hot paths

- **`slow`** - Tests taking >10s (typically long-running integrations)

  - Run with: `uv run pytest -m slow`
  - Examples: Long-running stress tests, full stack E2E tests

## Coverage

### Coverage Requirements

The project enforces **100% branch coverage** on the `provide.terminal` package:

```bash
uv run pytest --cov=provide.terminal --cov-branch --cov-fail-under=100
```

This is automatically checked in the pytest gate and CI.

### Excluding Tests from Coverage

Memory profiling tests (memray) run with `--no-cov` to avoid inflating coverage statistics since they focus on performance characteristics, not code coverage.

## Memory Profiling

Memory profiling uses [memray](https://github.com/bloomberg/memray) to detect allocation regressions in hot-path components (ANSI color processing, ControlChannel buffering, TermHub event management).

### Running Memory Tests Locally

Run all memray tests:

```bash
uv run pytest tests/memray/ -m memray -v --no-cov
```

Run a single stress test:

```bash
uv run pytest tests/memray/test_ansi_stress.py -m memray -v --no-cov
```

### Analyzing Memray Output

View detailed flamegraph and stats from a memray binary file:

```bash
# View statistics
python -m memray stats memray-output/ansi_stress.bin

# View flamegraph in browser
python -m memray flamegraph memray-output/ansi_stress.bin
```

### Baseline Management

Baseline allocation counts are stored in `tests/memray/baselines.json`.

#### First Run (Establishing Baseline)

On the first run, memray tests will record allocation counts and pass without comparison:

```bash
uv run pytest tests/memray/ -m memray -v --no-cov
```

#### Subsequent Runs (Regression Detection)

Allocations are compared to baseline with a **15% tolerance**. If a test exceeds the baseline by >15%, the test fails:

```
AssertionError: ANSI allocation 5200000 exceeds baseline 4500000 by 15.6% (tolerance: 15%)
```

#### Updating Baselines

After intentional optimizations, update baselines:

```bash
MEMRAY_UPDATE_BASELINE=1 uv run pytest tests/memray/ -m memray -v --no-cov
```

This updates `tests/memray/baselines.json` with new allocation counts.

#### Baseline File Format

```json
{
  "ansi_total_allocations": 4500000,
  "controlstream_total_allocations": 1000000,
  "hub_total_allocations": 280000
}
```

## CI/CD Integration

### Continuous Integration Workflow

The GitHub Actions workflow (`.github/workflows/ci.yml`) includes:

1. **quality** - Linting, type checking, formatting (main gate)
1. **mutation-gate** - Mutation testing on changed code
1. **performance-smoke** - Performance regression detection (scheduled)
1. **memory-regression** - Memory allocation regression detection (scheduled)

### Memory Regression Job

**Trigger:** Nightly schedule (2 AM UTC) or manual `workflow_dispatch`

**Command:** `uv run pytest tests/memray/ -m memray -v --tb=short --no-cov`

**Artifacts:** Uploaded memray `.bin` files for 30 days

**View Results:**

- GitHub Actions tab → memory-regression job → Artifacts
- Download memray output and analyze locally with `memray stats` or `memray flamegraph`

### Example: Triggering Memory Regression Manually

1. Navigate to: https://github.com/provide-io/provide-terminal/actions/workflows/ci.yml
1. Click "Run workflow"
1. Select branch
1. Choose "memory-regression" from job selector (if available)
1. Watch for completion; download artifacts if needed

## Mutation Testing

Mutation testing validates test quality by introducing small code changes (mutations) and verifying tests catch them.

### Running Mutation Tests

For changed files (recommended for PRs):

```bash
uv run python scripts/run_mutation_gate.py --changed-only --min-mutation-score 100
```

For full suite:

```bash
uv run python scripts/run_mutation_gate.py --min-mutation-score 100
```

### Mutation Test Configuration

See `pyproject.toml` `[tool.mutmut]` section for:

- `paths_to_mutate`: Which files to mutate
- `tests_dir`: Where to find tests
- `do_not_mutate`: Files to exclude (typically frontend, transports)

## Development Workflow

### Recommended Local Workflow

Before pushing or creating a PR:

```bash
# 1. Run standard test gate (fast, covers main functionality)
uv run python scripts/run_pytest_gate.py -q

# 2. Run mutation tests on changed files (validates test quality)
uv run python scripts/run_mutation_gate.py --changed-only --min-mutation-score 100

# 3. Run memray tests locally (optional, ~15-30 min)
MEMRAY_UPDATE_BASELINE=1 uv run pytest tests/memray/ -m memray -v --no-cov
```

### Playwright Tests

For frontend/UI changes:

```bash
# Install browser first
uv run playwright install chromium

# Run playwright tests
uv run pytest -m playwright
```

## Troubleshooting

### Memray Tests Fail: "Could not parse allocations"

The memray stats output format may have changed. Verify memray version:

```bash
python -m memray --version
```

And check the stats output format:

```bash
python -m memray stats memray-output/*.bin
```

### Coverage Requirement Not Met

If coverage drops below 100%, identify uncovered lines:

```bash
uv run pytest --cov=provide.terminal --cov-branch --cov-report=term-missing
```

Then either:

- Add test cases for uncovered branches
- Use `# pragma: no cover` for intentionally untested code (e.g., error paths in logging)

### Slow Tests Timeout

Increase the timeout:

```bash
uv run pytest -m slow --timeout=300
```

Or skip slow tests:

```bash
uv run pytest -m "not slow"
```

## See Also

- [README.md](../README.md) - Project overview and quality guarantees
- `.github/workflows/ci.yml` - Complete CI workflow definition
- `pyproject.toml` - Test configuration and markers
- `scripts/run_pytest_gate.py` - Main test runner
- `scripts/run_mutation_gate.py` - Mutation testing orchestrator
