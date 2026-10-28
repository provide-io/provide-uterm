/* packages/provide-uterm-platform/native/pam_uterm/pam_uterm.c
 * SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * PAM session module for provide-uterm daemon bridge.
 *
 * Install:
 *   sudo cp pam_uterm.so /usr/lib/security/        (Linux)
 *   sudo cp pam_uterm.so /usr/lib/pam/             (macOS)
 *
 * /etc/pam.d/sshd — add last in session section:
 *
 *   Notify mode (default): server receives login events, creates a companion shell.
 *     session  optional  pam_uterm.so
 *
 *   Capture mode: server observes the real SSH shell through a per-session socket.
 *     session  optional  pam_uterm.so mode=capture
 *   Add lib=/usr/lib/libuterm_capture.so when PAM should also inject LD_PRELOAD.
 *
 * Args:
 *   socket=PATH   notify socket (default /run/uterm-notify.sock)
 *   mode=MODE     "notify" (default) or "capture"
 *   lib=PATH      optional libuterm_capture.so path to inject in capture mode
 *   cap_dir=DIR   dir for per-pid capture sockets (default /run)
 *
 * JSON payloads sent to the notify socket (newline-terminated):
 *
 *   notify mode:
 *     open:  {"event":"open",  "username":"alice","tty":"/dev/pts/3","pid":12345,"mode":"notify",
 *             "start_ticks":1234567890}
 *     close: {"event":"close", "username":"alice","tty":"/dev/pts/3","pid":12345,"mode":"notify"}
 *
 *   capture mode (open adds capture_socket, start_ticks, and ssh_auth_info):
 *     open:  {"event":"open",  "username":"alice","tty":"/dev/pts/3","pid":12345,"mode":"capture",
 *             "capture_socket":"/run/uterm-cap-12345.sock",
 *             "ssh_auth_info":["publickey ssh-ed25519 AAAA...","publickey ssh-rsa AAAA..."],
 *             "start_ticks":1234567890}
 *     close: {"event":"close", "username":"alice","tty":"/dev/pts/3","pid":12345,"mode":"capture"}
 *
 *   ssh_auth_info is present only when sshd sets SSH_AUTH_INFO_0 (ExposeAuthInfo
 *   yes). The value is either a temp-file path (older sshd) or the auth info
 *   directly (OpenSSH 10.x+); the Python side fingerprints the keys and never
 *   receives the raw blob.
 *   start_ticks is field 22 of /proc/<pid>/stat (clock ticks since boot); it
 *   disambiguates pids (a pid can be reused after a process exits).
 *
 * Login record (capture mode only): before notifying the open event,
 * pam_uterm writes the same JSON line to <cap_dir>/uterm-cap-<pid>.login
 * (O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, mode 0640, group inherited from the
 * setgid capture directory). At startup uterm scans these records to adopt
 * logins that opened while it was down. pam_sm_close_session unlinks it.
 * Records live in /run and vanish at reboot.
 *
 * Non-fatal: PAM_SUCCESS is always returned so the session proceeds normally
 * even if the provide-uterm daemon is not running.
 */

#include <security/pam_appl.h>
#include <security/pam_modules.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define DEFAULT_SOCKET  "/run/uterm-notify.sock"
#define DEFAULT_CAP_DIR "/run"
#define MAX_PATH        256
#define MAX_ENV         512  /* "KEY=value" — must hold prefix + MAX_PATH */
/* Escaped-JSON field buffer: a username/tty (<= 256 bytes) where every byte
 * expands to a 6-char \uXXXX escape, plus the trailing NUL. */
#define MAX_JSON_FIELD  (6 * 256 + 1)
/* ── arg parsing ────────────────────────────────────────────────────────── */

static const char *_get_arg(int argc, const char **argv, const char *key, const char *def) {
    size_t klen = strlen(key);
    for (int i = 0; i < argc; i++) {
        if (strncmp(argv[i], key, klen) == 0 && argv[i][klen] == '=') {
            return argv[i] + klen + 1;
        }
    }
    return def;
}

static int _is_capture_mode(int argc, const char **argv) {
    const char *mode = _get_arg(argc, argv, "mode", "notify");
    return strcmp(mode, "capture") == 0;
}

/* ── JSON builder (dynamic allocation — no truncation risk) ──────────────── */

static char *_build_json(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int len = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    if (len < 0) return NULL;
    char *buf = malloc((size_t)len + 2);  /* +1 NUL, +1 newline */
    if (!buf) return NULL;
    va_start(ap, fmt);
    vsnprintf(buf, (size_t)len + 1, fmt, ap);
    va_end(ap);
    buf[len] = '\n';
    buf[len + 1] = '\0';
    return buf;
}

/* ── JSON string escaping ────────────────────────────────────────────────── */

/* Escape arbitrary bytes into a JSON string *body* (no surrounding quotes) per
 * RFC 8259: ", \, and control characters (< 0x20) are escaped. Without this a
 * username or tty containing a quote or newline (e.g. `alice"\n{"username":
 * "root"}`) could break out of its field and inject a second newline-delimited
 * event line into the notify stream. The result is written to dst (capacity
 * dstsize), truncated at a complete escape boundary on overflow, and is always
 * NUL-terminated. src may be NULL (treated as ""). */
static void _json_escape(char *dst, size_t dstsize, const char *src) {
    if (dstsize == 0) return;
    size_t o = 0;
    if (!src) src = "";
    for (const unsigned char *p = (const unsigned char *)src; *p; p++) {
        char unicode[7];
        const char *rep;
        size_t replen;
        unsigned char c = *p;
        switch (c) {
            case '"':  rep = "\\\""; replen = 2; break;
            case '\\': rep = "\\\\"; replen = 2; break;
            case '\b': rep = "\\b";  replen = 2; break;
            case '\f': rep = "\\f";  replen = 2; break;
            case '\n': rep = "\\n";  replen = 2; break;
            case '\r': rep = "\\r";  replen = 2; break;
            case '\t': rep = "\\t";  replen = 2; break;
            default:
                if (c < 0x20) {
                    snprintf(unicode, sizeof(unicode), "\\u%04x", c);
                    rep = unicode;
                    replen = 6;
                } else {
                    unicode[0] = (char)c;
                    unicode[1] = '\0';
                    rep = unicode;
                    replen = 1;
                }
        }
        if (o + replen + 1 > dstsize) break;  /* leave room for NUL; truncate clean */
        memcpy(dst + o, rep, replen);
        o += replen;
    }
    dst[o] = '\0';
}

/* ── unix socket notifier ────────────────────────────────────────────────── */

static void _notify(const char *socket_path, const char *json) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return;

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, socket_path, sizeof(addr.sun_path) - 1);

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
        ssize_t n = write(fd, json, strlen(json));
        (void)n;  /* best-effort notify — partial/failed writes are acceptable */
    }
    close(fd);
}

/* Publish the per-session socket independently from optional library
 * injection.  A constrained launcher may validate its final process before it
 * applies LD_PRELOAD, while existing deployments may still request injection
 * directly from PAM with lib=PATH. */
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

/* Read the process start time (field 22 of /proc/<pid>/stat, in clock ticks
 * since boot). Used to disambiguate pids: a pid can be reused after a process
 * exits, but (pid, start_ticks) is unique for a live login. Returns 0 on
 * failure (the Python side treats 0 as "unknown" and reads /proc itself). */
static unsigned long _read_start_ticks(int pid) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/stat", pid);
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    char buf[4096];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    if (n == 0) return 0;
    buf[n] = '\0';
    /* Field 2 (comm) is in parentheses and may contain spaces and even ')'.
     * Skip past the LAST ')' to reach the remaining space-separated fields. */
    char *p = strrchr(buf, ')');
    if (!p) return 0;
    p++;  /* now points to " state ppid pgrp ..." (field 3 onward) */
    /* Field 22 (starttime) is 20 spaces past ')': fields 3..21 are 19 fields,
     * and the 20th space lands us on field 22. */
    for (int i = 0; i < 20; i++) {
        p = strchr(p, ' ');
        if (!p) return 0;
        p++;
    }
    unsigned long ticks = 0;
    sscanf(p, "%lu", &ticks);
    return ticks;
}

/* Read SSH_AUTH_INFO_0 (a temp-file path set by sshd when ExposeAuthInfo=yes),
 * read the file, and return its lines as a JSON array string (malloc'd, or
 * NULL when the env var is absent or the file cannot be read). The file
 * contains lines like "publickey ssh-ed25519 AAAA..."; at most a handful
 * (sshd caps the number). Each line is JSON-escaped into a dynamic buffer so
 * no key is truncated. The raw keys leave this function only as JSON-escaped
 * array elements; the Python side fingerprints them and never receives the
 * raw blob. */
static char *_read_ssh_auth_info(pam_handle_t *pamh) {
    /* sshd sets SSH_AUTH_INFO_0 in the child process environment (via
     * child_set_env in session.c) but NOT in the PAM environment (it never
     * calls pam_setenv for it). pam_getenv reads only the PAM environment, so
     * it returns NULL; getenv reads the process environment and finds it. Try
     * the PAM environment first (a future sshd or a pam_env rule could set it
     * there), then fall back to the process environment.
     *
     * The value is either a path to a temp file (older sshd with
     * ExposeAuthInfo=yes) or the auth info directly — one key line per line
     * (OpenSSH 10.x+). Try fopen first (handles the file-path case); if it
     * fails, treat the value itself as the auth info via fmemopen (handles
     * the direct-value case). */
    const char *val = pam_getenv(pamh, "SSH_AUTH_INFO_0");
    if (!val || !*val) val = getenv("SSH_AUTH_INFO_0");
    if (!val || !*val) return NULL;
    FILE *f = fopen(val, "r");
    if (!f) {
        /* Not a readable file path — treat val as the auth info directly. */
        f = fmemopen((void *)val, strlen(val), "r");
        if (!f) return NULL;
    }

    size_t cap = 4096;
    char *buf = malloc(cap);
    if (!buf) { fclose(f); return NULL; }
    size_t len = 0;
    buf[len++] = '[';
    int first = 1;

    char line[8192];
    while (fgets(line, sizeof(line), f)) {
        /* Strip trailing newline / carriage return. */
        size_t l = strlen(line);
        while (l > 0 && (line[l-1] == '\n' || line[l-1] == '\r')) line[--l] = '\0';
        if (l == 0) continue;

        /* Ensure capacity: comma + quotes + escaped line (6x worst case) + 1. */
        size_t need = (first ? 0 : 1) + 2 + l * 6 + 1;
        if (len + need + 1 > cap) {
            cap = (len + need + 1) * 2;
            char *nb = realloc(buf, cap);
            if (!nb) { free(buf); fclose(f); return NULL; }
            buf = nb;
        }

        if (!first) buf[len++] = ',';
        first = 0;
        buf[len++] = '"';
        /* JSON-escape the line (same rules as _json_escape, inlined for the
         * dynamic buffer: base64 key data has no special chars, but a
         * crafted line must not break out of its string). */
        for (size_t i = 0; i < l; i++) {
            unsigned char c = (unsigned char)line[i];
            switch (c) {
                case '"':  buf[len++] = '\\'; buf[len++] = '"'; break;
                case '\\': buf[len++] = '\\'; buf[len++] = '\\'; break;
                case '\b': buf[len++] = '\\'; buf[len++] = 'b'; break;
                case '\f': buf[len++] = '\\'; buf[len++] = 'f'; break;
                case '\n': buf[len++] = '\\'; buf[len++] = 'n'; break;
                case '\r': buf[len++] = '\\'; buf[len++] = 'r'; break;
                case '\t': buf[len++] = '\\'; buf[len++] = 't'; break;
                default:
                    if (c < 0x20) {
                        len += (size_t)snprintf(buf + len, cap - len, "\\u%04x", c);
                    } else {
                        buf[len++] = (char)c;
                    }
            }
        }
        buf[len++] = '"';
    }
    fclose(f);
    buf[len++] = ']';
    buf[len] = '\0';
    return buf;
}

/* ── login record (Decision 3: adoption) ────────────────────────────────────── */

/* In mode=capture, before notifying, pam_uterm writes the open event's JSON
 * line (the same bytes, start_ticks and ssh_auth_info included) to
 * <cap_dir>/uterm-cap-<pid>.login. At startup uterm scans these records to
 * adopt logins it never heard of (a login that opened while uterm was down).
 *
 * The file is created as root with O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, mode
 * 0640, its group inherited from the setgid capture directory. A file already
 * at that path (a pid reused after a crash) is unlinked first. pam_sm_close
 * unlinks it. Records live in /run and vanish at reboot.
 *
 * The record is trusted no further than a PAM event: uterm's scan lstat's it
 * (regular file, uid 0, not group/world-writable, <=16 KiB) and verifies the
 * capture_socket and start_ticks against /proc/<pid>/stat before adopting. A
 * capture-group member can create files in the directory but cannot create one
 * owned by root, so a forged record needs root.
 */

/* Build the login-record path for pid in cap_dir. Returns 0 on success, -1 on
 * overflow. The path is the capture socket's path with .sock replaced by
 * .login — so the two are name-paired and a scan finds both. */
static int _login_record_path(char *dst, size_t dstsize,
                              const char *cap_dir, int pid) {
    int n = snprintf(dst, dstsize, "%s/uterm-cap-%d.login", cap_dir, pid);
    return (n < 0 || (size_t)n >= dstsize) ? -1 : 0;
}

/* Write the open event's JSON line to the login record. json must be the
 * NUL-terminated, newline-ended string _build_json produces. Best-effort: a
 * failure is logged to stderr (PAM modules have no other channel) and the
 * session proceeds — the notify socket is still the primary path. */
static void _write_login_record(const char *cap_dir, int pid, const char *json) {
    char path[MAX_PATH];
    if (_login_record_path(path, sizeof(path), cap_dir, pid) < 0) return;

    /* Stat the capture directory to inherit its group (setgid or otherwise).
     * The directory may not exist yet (uterm not started); without a group to
     * inherit we still write the record root-owned, which the scan accepts. */
    struct stat dir_st;
    int have_dir_gid = 0;
    gid_t dir_gid = 0;
    if (stat(cap_dir, &dir_st) == 0) {
        dir_gid = dir_st.st_gid;
        have_dir_gid = 1;
    }

    /* A file already at that path: a pid reused after a crash. Unlink it first
     * so O_EXCL succeeds and the record reflects this login, not the old one.
     * O_NOFOLLOW means a symlink left by an attacker is refused, not followed. */
    (void)unlink(path);

    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0640);
    if (fd < 0) {
        /* ENOENT: the capture directory does not exist (uterm not started, or
         * a misconfigured cap_dir). The record is best-effort; the notify
         * socket is still the primary path. */
        return;
    }

    /* Pin the mode to 0640 regardless of umask (root's umask is often 022,
     * which leaves 0640 intact, but 077 would strip the group read). */
    (void)fchmod(fd, 0640);

    /* Inherit the directory's group so capture-group members can read the
     * record (the scan runs as uterm's user, which may be a group member).
     * Only the group changes; the owner stays root (we are root). */
    if (have_dir_gid) {
        if (fchown(fd, (uid_t)-1, dir_gid) != 0) {
            /* A failure to set the group is not fatal: the record is still
             * root-owned and readable by root, and the scan can still lstat
             * it. The setgid directory usually inherits the group via the
             * kernel anyway, so this is belt-and-braces. */
        }
    }

    size_t total = strlen(json);
    size_t off = 0;
    while (off < total) {
        ssize_t n = write(fd, json + off, total - off);
        if (n < 0) {
            if (errno == EINTR) continue;
            break;  /* best-effort */
        }
        if (n == 0) break;
        off += (size_t)n;
    }
    (void)close(fd);
}

/* Remove the login record. Called from pam_sm_close_session. Best-effort: a
 * missing record (never written, or already removed) is a no-op. O_NOFOLLOW is
 * not needed here — unlink removes any directory entry, including a symlink,
 * without following it. */
static void _unlink_login_record(const char *cap_dir, int pid) {
    char path[MAX_PATH];
    if (_login_record_path(path, sizeof(path), cap_dir, pid) < 0) return;
    (void)unlink(path);
}

/* ── open session ────────────────────────────────────────────────────────── */

PAM_EXTERN int pam_sm_open_session(pam_handle_t *pamh, int flags __attribute__((unused)),
                                    int argc, const char **argv) {
    const char *socket_path = _get_arg(argc, argv, "socket", DEFAULT_SOCKET);
    const char *cap_dir     = _get_arg(argc, argv, "cap_dir", DEFAULT_CAP_DIR);
    const char *lib_path    = _get_arg(argc, argv, "lib", NULL);
    int capture             = _is_capture_mode(argc, argv);
    int pid                 = (int)getpid();

    /* pam_get_user() is the safe API for retrieving the username from within a
     * module — it avoids the internal PAM lock that pam_get_item(PAM_USER) can
     * deadlock on in some libpam implementations (e.g. Debian Linux-PAM). */
    const char *username = NULL;
    pam_get_user(pamh, &username, NULL);

    /* PAM_TTY is safe to read via pam_get_item from a session module. */
    const char *tty = NULL;
    pam_get_item(pamh, PAM_TTY, (const void **)&tty);

    /* Escape user-influenced fields so a crafted username/tty cannot break out
     * of its JSON string or inject a second newline-delimited event line. */
    char user_esc[MAX_JSON_FIELD];
    char tty_esc[MAX_JSON_FIELD];
    _json_escape(user_esc, sizeof(user_esc), username);
    _json_escape(tty_esc, sizeof(tty_esc), tty);

    char *json;
    if (capture) {
        char cap_sock[MAX_PATH];
        snprintf(cap_sock, sizeof(cap_sock), "%s/uterm-cap-%d.sock", cap_dir, pid);

        unsigned long start_ticks = _read_start_ticks(pid);
        char *auth_info = _read_ssh_auth_info(pamh);

        /* When ssh_auth_info is present, include it; otherwise omit the field
         * (the Python side treats absence as "no keys offered"). start_ticks is
         * always sent so the Python side can build the session id without a
         * separate /proc read. */
        if (auth_info) {
            json = _build_json(
                     "{\"event\":\"open\",\"username\":\"%s\",\"tty\":\"%s\","
                     "\"pid\":%d,\"mode\":\"capture\",\"capture_socket\":\"%s\","
                     "\"ssh_auth_info\":%s,\"start_ticks\":%lu}",
                     user_esc, tty_esc, pid, cap_sock, auth_info, start_ticks);
            free(auth_info);
        } else {
            json = _build_json(
                     "{\"event\":\"open\",\"username\":\"%s\",\"tty\":\"%s\","
                     "\"pid\":%d,\"mode\":\"capture\",\"capture_socket\":\"%s\","
                     "\"start_ticks\":%lu}",
                     user_esc, tty_esc, pid, cap_sock, start_ticks);
        }

        _set_capture_environment(pamh, lib_path, cap_sock);
    } else {
        unsigned long start_ticks = _read_start_ticks(pid);
        json = _build_json(
                 "{\"event\":\"open\",\"username\":\"%s\",\"tty\":\"%s\","
                 "\"pid\":%d,\"mode\":\"notify\",\"start_ticks\":%lu}",
                 user_esc, tty_esc, pid, start_ticks);
    }

    if (json) {
        /* In capture mode, write the login record before notifying so uterm
         * can adopt this login if the notify write fails or uterm was down.
         * The record is the same JSON line (start_ticks and ssh_auth_info
         * included), written to <cap_dir>/uterm-cap-<pid>.login. (Decision 3.) */
        if (capture) {
            _write_login_record(cap_dir, pid, json);
        }
        _notify(socket_path, json);
        free(json);
    }
    return PAM_SUCCESS;
}

/* ── close session ───────────────────────────────────────────────────────── */

PAM_EXTERN int pam_sm_close_session(pam_handle_t *pamh, int flags __attribute__((unused)),
                                     int argc, const char **argv) {
    const char *socket_path = _get_arg(argc, argv, "socket", DEFAULT_SOCKET);
    const char *cap_dir     = _get_arg(argc, argv, "cap_dir", DEFAULT_CAP_DIR);
    int capture             = _is_capture_mode(argc, argv);
    int pid                 = (int)getpid();

    const char *username = NULL;
    pam_get_user(pamh, &username, NULL);
    const char *tty = NULL;
    pam_get_item(pamh, PAM_TTY, (const void **)&tty);

    char user_esc[MAX_JSON_FIELD];
    char tty_esc[MAX_JSON_FIELD];
    _json_escape(user_esc, sizeof(user_esc), username);
    _json_escape(tty_esc, sizeof(tty_esc), tty);

    char *json = _build_json(
             "{\"event\":\"close\",\"username\":\"%s\",\"tty\":\"%s\","
             "\"pid\":%d,\"mode\":\"%s\"}",
             user_esc, tty_esc, pid,
             capture ? "capture" : "notify");

    if (json) {
        _notify(socket_path, json);
        free(json);
    }

    /* In capture mode, remove the login record. A missing record (never
     * written, or already removed) is a no-op. (Decision 3.) */
    if (capture) {
        _unlink_login_record(cap_dir, pid);
    }
    return PAM_SUCCESS;
}
