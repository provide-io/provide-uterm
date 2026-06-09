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
 *   Capture mode: server observes the real SSH shell via LD_PRELOAD interception.
 *     session  optional  pam_uterm.so mode=capture lib=/usr/lib/libuterm_capture.so
 *
 * Args:
 *   socket=PATH   notify socket (default /run/uterm-notify.sock)
 *   mode=MODE     "notify" (default) or "capture"
 *   lib=PATH      path to libuterm_capture.so (required for mode=capture)
 *   cap_dir=DIR   dir for per-pid capture sockets (default /run)
 *
 * JSON payloads sent to the notify socket (newline-terminated):
 *
 *   notify mode:
 *     open:  {"event":"open",  "username":"alice","tty":"/dev/pts/3","pid":12345,"mode":"notify"}
 *     close: {"event":"close", "username":"alice","tty":"/dev/pts/3","pid":12345,"mode":"notify"}
 *
 *   capture mode (open adds capture_socket; also sets LD_PRELOAD env via pam_putenv):
 *     open:  {"event":"open",  "username":"alice","tty":"/dev/pts/3","pid":12345,"mode":"capture",
 *             "capture_socket":"/run/uterm-cap-12345.sock"}
 *     close: {"event":"close", "username":"alice","tty":"/dev/pts/3","pid":12345,"mode":"capture"}
 *
 * Non-fatal: PAM_SUCCESS is always returned so the session proceeds normally
 * even if the provide-uterm daemon is not running.
 */

#include <security/pam_appl.h>
#include <security/pam_modules.h>
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
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

        json = _build_json(
                 "{\"event\":\"open\",\"username\":\"%s\",\"tty\":\"%s\","
                 "\"pid\":%d,\"mode\":\"capture\",\"capture_socket\":\"%s\"}",
                 user_esc, tty_esc, pid, cap_sock);

        /* Inject LD_PRELOAD and capture socket path into the PAM environment.
         * These are inherited by the child shell that sshd/login spawns.      */
        if (lib_path && *lib_path) {
            char preload[MAX_ENV];
            snprintf(preload, sizeof(preload), "LD_PRELOAD=%s", lib_path);
            pam_putenv(pamh, preload);

            char cap_env[MAX_ENV];
            snprintf(cap_env, sizeof(cap_env), "UTERM_CAPTURE_SOCKET=%s", cap_sock);
            pam_putenv(pamh, cap_env);
        }
    } else {
        json = _build_json(
                 "{\"event\":\"open\",\"username\":\"%s\",\"tty\":\"%s\","
                 "\"pid\":%d,\"mode\":\"notify\"}",
                 user_esc, tty_esc, pid);
    }

    if (json) {
        _notify(socket_path, json);
        free(json);
    }
    return PAM_SUCCESS;
}

/* ── close session ───────────────────────────────────────────────────────── */

PAM_EXTERN int pam_sm_close_session(pam_handle_t *pamh, int flags __attribute__((unused)),
                                     int argc, const char **argv) {
    const char *socket_path = _get_arg(argc, argv, "socket", DEFAULT_SOCKET);
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
    return PAM_SUCCESS;
}
