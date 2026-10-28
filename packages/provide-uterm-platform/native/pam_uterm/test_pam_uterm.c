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

    /* ── login record (Decision 3: adoption) ──────────────────────────────── */

    /* _login_record_path builds <cap_dir>/uterm-cap-<pid>.login. */
    char path_buf[MAX_PATH];
    expect_true("login_record_path builds",
                _login_record_path(path_buf, sizeof(path_buf), "/run", 4242) == 0);
    expect_eq("login_record_path value", path_buf, "/run/uterm-cap-4242.login");

    /* _write_login_record creates a regular file, mode 0640, with the JSON
     * line as its contents (newline included). _unlink_login_record removes
     * it. The test runs as a regular user, so the owner check is the current
     * uid (root-owned is the real-world invariant; the scan checks uid 0). */
    char tmpl[] = "/tmp/uterm-pam-selftest-XXXXXX";
    char *tmpdir = mkdtemp(tmpl);
    expect_true("mkdtemp", tmpdir != NULL);

    char rec_path[MAX_PATH];
    expect_true("login_record_path in tmpdir",
                _login_record_path(rec_path, sizeof(rec_path), tmpdir, 4242) == 0);

    const char *rec_json =
        "{\"event\":\"open\",\"username\":\"alice\",\"tty\":\"/dev/pts/3\","
        "\"pid\":4242,\"mode\":\"capture\","
        "\"capture_socket\":\"/tmp/uterm-cap-4242.sock\","
        "\"start_ticks\":1234567890}\n";

    _write_login_record(tmpdir, 4242, rec_json);

    struct stat rst;
    expect_true("login record exists", stat(rec_path, &rst) == 0);
    expect_true("login record is regular file", S_ISREG(rst.st_mode));
    /* Mode 0640, possibly with the setgid bit inherited from a setgid temp
     * dir (mkdtemp makes 0700, so no setgid here). Mask the type bits. */
    expect_true("login record mode 0640",
                (rst.st_mode & 07777) == 0640);
    /* Owner is the current uid (root in production; the scan checks uid 0). */
    expect_true("login record owner is current uid",
                rst.st_uid == getuid());

    /* Contents are the exact JSON line, newline included. */
    FILE *rf = fopen(rec_path, "r");
    expect_true("login record readable", rf != NULL);
    if (rf) {
        char contents[1024];
        size_t nread = fread(contents, 1, sizeof(contents) - 1, rf);
        contents[nread] = '\0';
        fclose(rf);
        expect_eq("login record contents", contents, rec_json);
    }

    /* O_EXCL replacement: a second write unlinks the first and creates a new
     * file (a pid reused after a crash). The file should still exist and
     * contain the new contents. */
    const char *rec_json2 =
        "{\"event\":\"open\",\"username\":\"bob\",\"tty\":\"/dev/pts/4\","
        "\"pid\":4242,\"mode\":\"capture\","
        "\"capture_socket\":\"/tmp/uterm-cap-4242.sock\","
        "\"start_ticks\":9876543210}\n";
    _write_login_record(tmpdir, 4242, rec_json2);
    expect_true("login record still exists after replace",
                stat(rec_path, &rst) == 0);

    rf = fopen(rec_path, "r");
    expect_true("login record readable after replace", rf != NULL);
    if (rf) {
        char contents[1024];
        size_t nread = fread(contents, 1, sizeof(contents) - 1, rf);
        contents[nread] = '\0';
        fclose(rf);
        expect_eq("login record contents after replace", contents, rec_json2);
    }

    /* unlink on close: _unlink_login_record removes the file. */
    _unlink_login_record(tmpdir, 4242);
    expect_true("login record unlinked",
                stat(rec_path, &rst) != 0 && errno == ENOENT);

    /* _unlink_login_record on a missing record is a no-op (no crash). */
    _unlink_login_record(tmpdir, 4242);
    _unlink_login_record(tmpdir, 99999);

    /* _write_login_record into a nonexistent directory is a no-op (the
     * capture directory may not exist yet — uterm not started). The notify
     * socket is still the primary path. */
    _write_login_record("/nonexistent-dir-uterm-test", 4242, rec_json);

    /* Cleanup the temp dir. */
    rmdir(tmpdir);

    if (failures == 0) {
        printf("pam_uterm self-test: all checks passed\n");
        return 0;
    }
    fprintf(stderr, "pam_uterm self-test: %d failure(s)\n", failures);
    return 1;
}
