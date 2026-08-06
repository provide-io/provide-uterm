# PAM Capture Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let capture-mode PAM sessions publish their capture socket without preloading Uterm into the intermediate login shell.

**Architecture:** `pam_uterm` will always export `UTERM_CAPTURE_SOCKET` in capture mode and will export `LD_PRELOAD` only when the administrator supplies a non-empty `lib=` argument. A focused native test will replace `pam_putenv` with a recorder and verify both environment contracts without opening a real PAM session.

**Tech Stack:** C11, Linux PAM, GNU Make, Uterm's existing native self-test harness

## Global Constraints

- Preserve notify-mode behavior and capture event JSON.
- Preserve explicit `lib=` support for existing deployments.
- Return `PAM_SUCCESS` when notification or environment injection fails, matching the module's non-fatal contract.
- Do not change other Uterm packages or language implementations because this behavior belongs to the Linux PAM adapter.
- Keep `.beads/interactions.jsonl` out of every commit.

---

### Task 1: Separate capture-socket publication from preload injection

**Files:**
- Modify: `packages/provide-uterm-platform/native/pam_uterm/pam_uterm.c`
- Modify: `packages/provide-uterm-platform/native/pam_uterm/test_pam_uterm.c`

**Interfaces:**
- Consumes: PAM's `pam_putenv(pam_handle_t *, const char *)` and the existing `lib=` and `cap_dir=` arguments.
- Produces: `static void _set_capture_environment(pam_handle_t *pamh, const char *lib_path, const char *cap_sock)`.

- [ ] **Step 1: Extend the native self-test with a `pam_putenv` recorder**

Include the PAM headers before the module source, replace `pam_putenv` while the source is included, and record each value:

```c
#include <security/pam_appl.h>

static int fake_pam_putenv(pam_handle_t *pamh, const char *value);

#define pam_putenv fake_pam_putenv
#include "pam_uterm.c"
#undef pam_putenv

static char recorded_env[2][MAX_ENV];
static size_t recorded_env_count;

static int fake_pam_putenv(pam_handle_t *pamh, const char *value) {
    (void)pamh;
    if (recorded_env_count < 2) {
        snprintf(recorded_env[recorded_env_count], MAX_ENV, "%s", value);
    }
    recorded_env_count++;
    return PAM_SUCCESS;
}
```

Add assertions for both supported configurations:

```c
recorded_env_count = 0;
_set_capture_environment((pam_handle_t *)1, NULL, "/run/uterm-cap-42.sock");
expect_true("socket-only count", recorded_env_count == 1);
expect_eq("socket-only value", recorded_env[0],
          "UTERM_CAPTURE_SOCKET=/run/uterm-cap-42.sock");

recorded_env_count = 0;
_set_capture_environment((pam_handle_t *)1, "/opt/uterm/libuterm_capture.so",
                         "/run/uterm-cap-43.sock");
expect_true("socket-and-lib count", recorded_env_count == 2);
expect_eq("socket first", recorded_env[0],
          "UTERM_CAPTURE_SOCKET=/run/uterm-cap-43.sock");
expect_eq("preload second", recorded_env[1],
          "LD_PRELOAD=/opt/uterm/libuterm_capture.so");
```

- [ ] **Step 2: Run the native self-test and verify the new expectations fail**

Run: `make clean test`

Working directory: `packages/provide-uterm-platform/native/pam_uterm`

Expected: compilation or assertion failure because `_set_capture_environment` does not exist and socket-only mode is not implemented.

- [ ] **Step 3: Implement the environment helper and call it from capture mode**

Add the helper above `pam_sm_open_session`:

```c
static void _set_capture_environment(pam_handle_t *pamh,
                                     const char *lib_path,
                                     const char *cap_sock) {
    char cap_env[MAX_ENV];
    snprintf(cap_env, sizeof(cap_env), "UTERM_CAPTURE_SOCKET=%s", cap_sock);
    pam_putenv(pamh, cap_env);

    if (lib_path && *lib_path) {
        char preload[MAX_ENV];
        snprintf(preload, sizeof(preload), "LD_PRELOAD=%s", lib_path);
        pam_putenv(pamh, preload);
    }
}
```

Replace the coupled block in `pam_sm_open_session` with:

```c
_set_capture_environment(pamh, lib_path, cap_sock);
```

Update the module comments so `lib=` is optional in capture mode and the socket is always published.

- [ ] **Step 4: Run the affected native gates**

Run:

```bash
make clean test
make
```

Working directory: `packages/provide-uterm-platform/native/pam_uterm`

Expected: `pam_uterm self-test: all checks passed`, followed by a warning-free `pam_uterm.so` build.

- [ ] **Step 5: Run the existing early-preload regression**

Run:

```bash
make clean test
```

Working directory: `packages/provide-uterm-platform/native/capture`

Expected: the capture writer tests, symbol tests, and pre-constructor preload executable all pass.

- [ ] **Step 6: Commit the Uterm change**

```bash
git add packages/provide-uterm-platform/native/pam_uterm/pam_uterm.c \
        packages/provide-uterm-platform/native/pam_uterm/test_pam_uterm.c
git diff --cached --check
git commit -S -m "fix: decouple PAM capture socket from preload"
```

Do not stage `.beads/interactions.jsonl`.

### Task 2: Verify the Uterm consumer boundary and close durable work

**Files:**
- Modify through `bd`: issue `provide-uterm-u4y`

**Interfaces:**
- Consumes: the native capture and PAM artifacts from Task 1.
- Produces: passing native checks, passing platform PTY tests, and a closed Beads issue with the verified revision recorded.

- [ ] **Step 1: Run the focused Python PAM and capture tests**

Run:

```bash
uv run --project packages/provide-uterm-platform pytest \
  packages/provide-uterm-platform/tests/pty/test_pam.py \
  packages/provide-uterm-platform/tests/pty/test_pam_listener.py \
  packages/provide-uterm-platform/tests/pty/test_capture.py \
  packages/provide-uterm-platform/tests/pty/test_ld_preload_capture.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Verify the committed scope and signature**

Run:

```bash
git diff --check HEAD^ HEAD
git log -1 --show-signature --format=fuller
git status --short
```

Expected: no diff errors, a good signature, and only the pre-existing `.beads/interactions.jsonl` modification.

- [ ] **Step 3: Close the Beads issue after an external consumer's live proof**

After an external consumer proves socket-only PAM configuration and bounded capture on the server, run:

```bash
bd close provide-uterm-u4y --reason="Pre-constructor capture, unique PAM IDs, socket-only PAM export, and the external consumer boundary passed native, Python, and live SSH verification."
```

Expected: `provide-uterm-u4y` is closed. Do not push the Dolt or Git remotes unless the user requests publication.
