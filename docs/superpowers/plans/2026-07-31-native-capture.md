# Native Capture Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent capture backpressure or short writes from blocking the observed process or corrupting subsequent frames.

**Architecture:** Keep the wire format but make the capture socket nonblocking and serialize complete frame attempts under a nonblocking mutex. Availability wins: if the socket would block, the frame is dropped and a counter increments. Short writes disable capture because a partially emitted length-prefixed frame cannot be repaired safely on a stream.

**Tech Stack:** Portable C11/POSIX, pthreads, UNIX sockets, Make.

---

### Task 1: Extract and test frame emission

**Files:**
- Create: `packages/provide-uterm-platform/native/capture/capture_writer.h`
- Create: `packages/provide-uterm-platform/native/capture/capture_writer.c`
- Create: `packages/provide-uterm-platform/native/capture/test_capture_writer.c`
- Modify: `packages/provide-uterm-platform/native/capture/Makefile`

- [ ] **Step 1: Write the failing C self-test**

Inject a scripted send function and assert: exact header/payload bytes; payload above `UINT32_MAX` is rejected; `EAGAIN` drops without retry; `EINTR` retries before any bytes are written; a positive short write disables the writer; two producers cannot interleave frames.

```bash
make test
```

Expected: fail because the writer API does not exist.

- [ ] **Step 2: Implement bounded serialized emission**

Expose an internal writer state with fd, send callback, try-lock, disabled flag, and dropped count. Use `pthread_mutex_trylock`; failure drops immediately. Build one bounded frame, attempt send, retry `EINTR` only when zero bytes were accepted, and disable after a short positive result or fatal socket error.

- [ ] **Step 3: Verify the self-test and commit**

```bash
make clean test all
git add packages/provide-uterm-platform/native/capture
git commit -m "fix(native): make capture emission nonblocking"
```

### Task 2: Integrate the writer into both interposition backends

**Files:**
- Modify: `packages/provide-uterm-platform/native/capture/capture.c`
- Modify: `packages/provide-uterm-platform/native/capture/Makefile`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/roadmap/uterm-code-review-remediation.md`

- [ ] **Step 1: Add a failing integration assertion**

Extend the self-test/build target so both platform compile branches typecheck the shared writer API where the host compiler supports them. Assert the initialized capture fd has `O_NONBLOCK`.

- [ ] **Step 2: Replace duplicate `send_frame` implementations**

Initialize the shared writer after connecting the UNIX socket, call it from macOS and Linux hooks, and remove the duplicated blocking frame builders. Set `O_NONBLOCK` with `fcntl` after connect. Destruction closes and disables the state safely.

- [ ] **Step 3: Add a native CI build/self-test step**

Run capture `make test all` on Linux and macOS jobs where toolchains are present; keep PAM integration separate because it requires host PAM headers and privileges.

- [ ] **Step 4: Verify, update tracker, and commit**

```bash
make clean test all
```

Complete `NATIVE-001` through `NATIVE-004` only with recorded build/test evidence.

