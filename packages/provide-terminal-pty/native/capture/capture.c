/* packages/undef-terminal-pty/native/capture/capture.c
 * SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * libuterm_capture: LD_PRELOAD / DYLD_INSERT_LIBRARIES capture library.
 *
 * Frame format: [1B channel][4B length big-endian][N bytes payload]
 * Channels: 0x01=stdout/stderr write, 0x02=stdin read, 0x03=connect addr
 *
 * Only activates when UTERM_CAPTURE_SOCKET env var is set.
 * Only intercepts fd 0/1/2 for read/write to avoid recursion —
 * internal writes to capture_fd (which is fd > 2) are never intercepted.
 *
 * macOS SIP note: DYLD_INSERT_LIBRARIES is blocked for system-signed
 * binaries (e.g. /usr/sbin/sshd). Use pam_uterm.so for sshd bridging.
 * Injection works for user-space binaries and non-SIP-protected targets.
 *
 * readline/libedit note: readline uses read(0,...) for input and write(1,...)
 * for prompts/output. Both are intercepted here. For PTY sessions, the PTY
 * master already captures the same bytes; inject=True adds value for
 * binaries that also do direct network I/O (captured via connect()).
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define CHANNEL_STDOUT  0x01
#define CHANNEL_STDIN   0x02
#define CHANNEL_CONNECT 0x03

static int g_capture_fd = -1;

typedef ssize_t (*fn_write)(int, const void *, size_t);
typedef ssize_t (*fn_read)(int, void *, size_t);
typedef int     (*fn_connect)(int, const struct sockaddr *, socklen_t);

#ifndef __APPLE__
static fn_write   orig_write;
static fn_read    orig_read;
static fn_connect orig_connect;
#endif

static void send_frame(uint8_t channel, const void *data, size_t len) {
    if (g_capture_fd < 0) return;
    uint8_t header[5];
    uint32_t n = (uint32_t)len;
    header[0] = channel;
    header[1] = (n >> 24) & 0xff;
    header[2] = (n >> 16) & 0xff;
    header[3] = (n >>  8) & 0xff;
    header[4] = (n      ) & 0xff;
    /* Write directly to capture_fd (fd > 2) — not intercepted by our write() */
    (void)write(g_capture_fd, header, 5);
    (void)write(g_capture_fd, data, len);
}

__attribute__((constructor))
static void uterm_capture_init(void) {
    const char *path = getenv("UTERM_CAPTURE_SOCKET");
    if (!path || !*path) return;

    g_capture_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (g_capture_fd < 0) return;

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

#ifdef __APPLE__
    if (connect(g_capture_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
#else
    fn_connect real_connect = (fn_connect)dlsym(RTLD_NEXT, "connect");
    orig_write   = (fn_write)  dlsym(RTLD_NEXT, "write");
    orig_read    = (fn_read)   dlsym(RTLD_NEXT, "read");
    orig_connect = real_connect;
    if (real_connect(g_capture_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
#endif
        close(g_capture_fd);
        g_capture_fd = -1;
    }
}

ssize_t write(int fd, const void *buf, size_t count) {
#ifdef __APPLE__
    fn_write real = (fn_write)dlsym(RTLD_NEXT, "write");
#else
    fn_write real = orig_write;
#endif
    ssize_t ret = real(fd, buf, count);
    if (ret > 0 && (fd == STDOUT_FILENO || fd == STDERR_FILENO)) {
        send_frame(CHANNEL_STDOUT, buf, (size_t)ret);
    }
    return ret;
}

ssize_t read(int fd, void *buf, size_t count) {
#ifdef __APPLE__
    fn_read real = (fn_read)dlsym(RTLD_NEXT, "read");
#else
    fn_read real = orig_read;
#endif
    ssize_t ret = real(fd, buf, count);
    if (ret > 0 && fd == STDIN_FILENO) {
        send_frame(CHANNEL_STDIN, buf, (size_t)ret);
    }
    return ret;
}

int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    /* Avoid recursion on our own init connect */
    if (sockfd == g_capture_fd) {
#ifdef __APPLE__
        fn_connect real = (fn_connect)dlsym(RTLD_NEXT, "connect");
#else
        fn_connect real = orig_connect;
#endif
        return real(sockfd, addr, addrlen);
    }

#ifdef __APPLE__
    fn_connect real = (fn_connect)dlsym(RTLD_NEXT, "connect");
#else
    fn_connect real = orig_connect;
#endif
    int ret = real(sockfd, addr, addrlen);
    if (ret == 0 && addr) {
        char addrstr[256] = {0};
        if (addr->sa_family == AF_INET) {
            const struct sockaddr_in *in4 = (const struct sockaddr_in *)addr;
            snprintf(addrstr, sizeof(addrstr), "%s:%d",
                     inet_ntoa(in4->sin_addr), ntohs(in4->sin_port));
        } else if (addr->sa_family == AF_INET6) {
            const struct sockaddr_in6 *in6 = (const struct sockaddr_in6 *)addr;
            char ip[INET6_ADDRSTRLEN];
            inet_ntop(AF_INET6, &in6->sin6_addr, ip, sizeof(ip));
            snprintf(addrstr, sizeof(addrstr), "[%s]:%d", ip, ntohs(in6->sin6_port));
        } else if (addr->sa_family == AF_UNIX) {
            const struct sockaddr_un *un = (const struct sockaddr_un *)addr;
            snprintf(addrstr, sizeof(addrstr), "unix:%s", un->sun_path);
        }
        if (addrstr[0]) send_frame(CHANNEL_CONNECT, addrstr, strlen(addrstr));
    }
    return ret;
}
