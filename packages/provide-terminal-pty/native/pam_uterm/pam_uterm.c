/* packages/undef-terminal-pty/native/pam_uterm/pam_uterm.c
 * SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * PAM session module for undef-terminal daemon bridge.
 *
 * Install:
 *   sudo cp pam_uterm.so /usr/lib/security/        (Linux)
 *   sudo cp pam_uterm.so /usr/lib/pam/             (macOS)
 *
 * /etc/pam.d/sshd — add last in session section:
 *   session  optional  pam_uterm.so
 *
 * Override socket path (default /run/uterm-notify.sock):
 *   session  optional  pam_uterm.so socket=/run/uterm-notify.sock
 *
 * JSON payloads:
 *   open:  {"event":"open",  "username":"alice","tty":"/dev/pts/3","pid":12345}
 *   close: {"event":"close", "username":"alice","tty":"/dev/pts/3","pid":12345}
 *
 * Non-fatal: if socket is unavailable, PAM_SUCCESS is returned so the
 * session proceeds normally without uterm bridging.
 */

#include <security/pam_appl.h>
#include <security/pam_modules.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define DEFAULT_SOCKET "/run/uterm-notify.sock"
#define MAX_JSON 512

static const char *get_socket_path(int argc, const char **argv) {
    for (int i = 0; i < argc; i++) {
        if (strncmp(argv[i], "socket=", 7) == 0) {
            return argv[i] + 7;
        }
    }
    return DEFAULT_SOCKET;
}

static void notify(const char *socket_path, const char *event,
                   const char *username, const char *tty, int pid) {
    char json[MAX_JSON];
    snprintf(json, sizeof(json),
             "{\"event\":\"%s\",\"username\":\"%s\",\"tty\":\"%s\",\"pid\":%d}\n",
             event,
             username ? username : "",
             tty      ? tty      : "",
             pid);

    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return;

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, socket_path, sizeof(addr.sun_path) - 1);

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
        (void)write(fd, json, strlen(json));
    }
    close(fd);
}

PAM_EXTERN int pam_sm_open_session(pam_handle_t *pamh, int flags __attribute__((unused)),
                                    int argc, const char **argv) {
    const char *socket_path = get_socket_path(argc, argv);
    const char *username = NULL;
    const char *tty = NULL;
    pam_get_item(pamh, PAM_USER, (const void **)&username);
    pam_get_item(pamh, PAM_TTY,  (const void **)&tty);
    notify(socket_path, "open", username, tty, getpid());
    return PAM_SUCCESS;
}

PAM_EXTERN int pam_sm_close_session(pam_handle_t *pamh, int flags __attribute__((unused)),
                                     int argc, const char **argv) {
    const char *socket_path = get_socket_path(argc, argv);
    const char *username = NULL;
    const char *tty = NULL;
    pam_get_item(pamh, PAM_USER, (const void **)&username);
    pam_get_item(pamh, PAM_TTY,  (const void **)&tty);
    notify(socket_path, "close", username, tty, getpid());
    return PAM_SUCCESS;
}
