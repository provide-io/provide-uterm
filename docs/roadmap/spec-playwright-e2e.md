# Specification: Playwright E2E Automation Suite

## Overview
While the project has high unit and integration coverage, an end-to-end (E2E) UI testing framework is needed to catch visual regressions, DOM layout issues, and full-stack orchestration bugs (e.g., typing in the browser -> websocket -> DO -> PTY -> broadcast back to browser).

## Requirements
- Integrate Playwright into the testing CI pipeline.
- Create core smoke tests for the primary "happy paths" (session creation, terminal interaction, approval flows).
- Ensure E2E tests run against a localized stack (using miniflare/workerd and a local fastapi instance).

## Scope
- Add a new `tests/e2e/` package/directory.
