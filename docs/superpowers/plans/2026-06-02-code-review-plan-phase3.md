# provide-uterm Code Review Plan Phase 3

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand the comprehensive code review and architectural analysis to cover the Native App Wrapper and the Annotation Layer.

**Architecture:** Systematically review the 2 target directories, evaluate against the four core lenses (Health, Maintainability, Security, Performance), and append the findings to the main artifact.

---

### Task 1: Research & Write Desktop/Native App Wrappers

**Files:**
- Modify: `artifacts/2026-06-02-code-review-report.md`

- [ ] **Step 1: Read and analyze App Wrapper code**
  Read the files in `packages/provide-uterm-app/`. Focus on IPC security boundaries, local filesystem access, and desktop-specific performance. Evaluate deeply against the 4 lenses.
  
- [ ] **Step 2: Append Part 7 of the Report**
  Write Part 7 of the report and append it to the end of `artifacts/2026-06-02-code-review-report.md`. Ensure consistency with the 4-lens format.

### Task 2: Research & Write Annotation Layer

**Files:**
- Modify: `artifacts/2026-06-02-code-review-report.md`

- [ ] **Step 1: Read and analyze Annotation code**
  Read files in `packages/provide-uterm-annotation/`. Focus on type-safety, telemetry schemas, event structures, and data privacy guarantees. Evaluate deeply against the 4 lenses.

- [ ] **Step 2: Append Part 8 of the Report**
  Write Part 8 and append it to the report artifact.

### Task 3: Finalize and Commit Phase 3

**Files:**
- Modify: `artifacts/2026-06-02-code-review-report.md`

- [ ] **Step 1: Review and refine**
  Ensure the entire report reads consistently and is nicely formatted.

- [ ] **Step 2: Commit changes**
  ```bash
  git add artifacts/2026-06-02-code-review-report.md
  git commit -m "docs: expand code review with phase 3 subsystems (app and annotation)"
  ```
