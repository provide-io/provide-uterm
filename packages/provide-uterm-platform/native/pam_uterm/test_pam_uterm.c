/* packages/provide-uterm-platform/native/pam_uterm/test_pam_uterm.c
 * SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Self-test for the static helpers in pam_uterm.c — chiefly _json_escape, which
 * neutralizes a username/tty that contains quotes or newlines so it cannot break
 * out of its JSON field or inject a second event line into the notify stream.
 *
 * Built and run via `make test` (the module is #include'd to reach the static
 * function; -lpam resolves the PAM session symbols that pulls in). The native
 * library is not built in CI, so this is a local correctness gate.
 */

#include <security/pam_appl.h>

static int fake_pam_putenv(pam_handle_t *pamh, const char *value);

#define pam_putenv fake_pam_putenv
#include "pam_uterm.c"
#undef pam_putenv

#include <stdio.h>
#include <string.h>

static int failures = 0;
static char recorded_env[2][MAX_ENV];
static size_t recorded_env_count = 0;

static int fake_pam_putenv(pam_handle_t *pamh, const char *value) {
    (void)pamh;
    if (recorded_env_count < 2) {
        snprintf(recorded_env[recorded_env_count], MAX_ENV, "%s", value);
    }
    recorded_env_count++;
    return PAM_SUCCESS;
}

static void expect_eq(const char *what, const char *got, const char *want) {
    if (strcmp(got, want) != 0) {
        fprintf(stderr, "FAIL %s: got \"%s\" want \"%s\"\n", what, got, want);
        failures++;
    }
}

static void expect_true(const char *what, int cond) {
    if (!cond) {
        fprintf(stderr, "FAIL %s\n", what);
        failures++;
    }
}

int main(void) {
    char out[MAX_JSON_FIELD];

    /* Plain text and NULL pass through unchanged. */
    _json_escape(out, sizeof(out), "alice");
    expect_eq("plain", out, "alice");
    _json_escape(out, sizeof(out), NULL);
    expect_eq("null->empty", out, "");

    /* Structural metacharacters are escaped. */
    _json_escape(out, sizeof(out), "a\"b");
    expect_eq("quote", out, "a\\\"b");
    _json_escape(out, sizeof(out), "a\\b");
    expect_eq("backslash", out, "a\\\\b");
    _json_escape(out, sizeof(out), "a\nb\tc\rd");
    expect_eq("ctrl-shorts", out, "a\\nb\\tc\\rd");
    _json_escape(out, sizeof(out), "x\x01y\x1f");
    expect_eq("unicode-escapes", out, "x\\u0001y\\u001f");

    /* The exact injection from the security claim is fully neutralized: no raw
     * newline survives and the quote is escaped, so it stays one JSON string. */
    _json_escape(out, sizeof(out), "alice\"\n{\"username\":\"root\"}");
    expect_true("no raw newline", strchr(out, '\n') == NULL);
    expect_true("quote escaped", strstr(out, "\\\"") != NULL);
    expect_true("newline escaped", strstr(out, "\\n") != NULL);

    /* Truncation safety: a tiny buffer never overflows and stays NUL-terminated
     * at a clean escape boundary (one full \" fits in 4 bytes, the next breaks). */
    char tiny[4];
    _json_escape(tiny, sizeof(tiny), "\"\"\"\"");
    expect_true("tiny stays bounded", strlen(tiny) <= sizeof(tiny) - 1);
    expect_eq("tiny truncates clean", tiny, "\\\"");

    /* dstsize 0 and 1 must not write past the buffer. */
    char one[1];
    _json_escape(one, sizeof(one), "x");
    expect_true("size-1 yields empty", one[0] == '\0');
    _json_escape(out, 0, "x"); /* must early-return without touching out */

    /* Capture mode always publishes its per-session socket.  Library
     * injection remains optional so a launcher may apply it only after it has
     * validated the final terminal process. */
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

    if (failures == 0) {
        printf("pam_uterm self-test: all checks passed\n");
        return 0;
    }
    fprintf(stderr, "pam_uterm self-test: %d failure(s)\n", failures);
    return 1;
}
