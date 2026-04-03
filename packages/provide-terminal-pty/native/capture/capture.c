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
 * macOS: uses DYLD_INTERPOSE (the only reliable interposition mechanism on
 * two-level-namespace binaries).  RTLD_NEXT under DYLD_INTERPOSE returns our
 * own replacement, so we retrieve the original function address from the
 * interpose struct's `.original` field, which holds the link-time address of
 * the target function (before dyld applies any patches).
 *
 * macOS SIP note: DYLD_INSERT_LIBRARIES is blocked for system-signed binaries
 * (e.g. /usr/sbin/sshd).  Use pam_uterm.so for sshd bridging.
 * Injection works for user-space binaries and non-SIP-protected targets.
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

#ifdef __APPLE__

/* ── macOS DYLD_INTERPOSE implementation ──────────────────────────────────────
 *
 * DYLD_INTERPOSE patches all call sites globally, including dlsym — so
 * RTLD_NEXT / RTLD_DEFAULT both resolve to our replacement, not the original.
 * The workaround: the interpose struct's .original field holds the link-time
 * address of the target function, set before dyld applies any patches.  We
 * read that address at constructor time to get a direct pointer to the real
 * implementation.
 */

typedef struct { const void *replacement; const void *original; } interpose_t;

/* Forward-declare replacement functions so their addresses can be used in the
 * interpose structs below. */
static ssize_t uterm_write(int, const void *, size_t);
static ssize_t uterm_read(int, void *, size_t);
static int     uterm_connect(int, const struct sockaddr *, socklen_t);

/* Interpose structs — placed in __DATA,__interpose.  The .original field is
 * set to the link-time address of the target symbol.  dyld reads these to
 * patch call sites; it does not modify the structs themselves. */
__attribute__((used, section("__DATA,__interpose")))
static const interpose_t _itp_write   = { (const void *)&uterm_write,   (const void *)&write };
__attribute__((used, section("__DATA,__interpose")))
static const interpose_t _itp_read    = { (const void *)&uterm_read,    (const void *)&read };
__attribute__((used, section("__DATA,__interpose")))
static const interpose_t _itp_connect = { (const void *)&uterm_connect, (const void *)&connect };

/* Originals resolved from the interpose structs in the constructor. */
static fn_write   g_real_write;
static fn_read    g_real_read;
static fn_connect g_real_connect;

static void send_frame(uint8_t channel, const void *data, size_t len) {
    if (g_capture_fd < 0 || !g_real_write) return;
    uint8_t header[5];
    uint32_t n = (uint32_t)len;
    header[0] = channel;
    header[1] = (n >> 24) & 0xff;
    header[2] = (n >> 16) & 0xff;
    header[3] = (n >>  8) & 0xff;
    header[4] = (n      ) & 0xff;
    /* Call real write directly (g_capture_fd > 2 so no recursion even if
     * g_real_write ended up as our hook — but it won't, per the above). */
    g_real_write(g_capture_fd, header, 5);
    g_real_write(g_capture_fd, data, len);
}

__attribute__((constructor))
static void uterm_capture_init(void) {
    /* Read originals from the interpose structs — these hold link-time addresses
     * set before dyld patched the call sites. */
    g_real_write   = (fn_write)  _itp_write.original;
    g_real_read    = (fn_read)   _itp_read.original;
    g_real_connect = (fn_connect)_itp_connect.original;

    const char *path = getenv("UTERM_CAPTURE_SOCKET");
    if (!path || !*path) return;

    g_capture_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (g_capture_fd < 0) return;

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    if (g_real_connect(g_capture_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(g_capture_fd);
        g_capture_fd = -1;
    }
}

static ssize_t uterm_write(int fd, const void *buf, size_t count) {
    ssize_t ret = g_real_write(fd, buf, count);
    if (ret > 0 && (fd == STDOUT_FILENO || fd == STDERR_FILENO)) {
        send_frame(CHANNEL_STDOUT, buf, (size_t)ret);
    }
    return ret;
}

static ssize_t uterm_read(int fd, void *buf, size_t count) {
    ssize_t ret = g_real_read(fd, buf, count);
    if (ret > 0 && fd == STDIN_FILENO) {
        send_frame(CHANNEL_STDIN, buf, (size_t)ret);
    }
    return ret;
}

static int uterm_connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    if (sockfd == g_capture_fd) return g_real_connect(sockfd, addr, addrlen);
    int ret = g_real_connect(sockfd, addr, addrlen);
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

#else  /* ── Linux LD_PRELOAD implementation ──────────────────────────────── */

static fn_write   orig_write;
static fn_read    orig_read;
static fn_connect orig_connect;

static void send_frame(uint8_t channel, const void *data, size_t len) {
    if (g_capture_fd < 0 || !orig_write) return;
    uint8_t header[5];
    uint32_t n = (uint32_t)len;
    header[0] = channel;
    header[1] = (n >> 24) & 0xff;
    header[2] = (n >> 16) & 0xff;
    header[3] = (n >>  8) & 0xff;
    header[4] = (n      ) & 0xff;
    orig_write(g_capture_fd, header, 5);
    orig_write(g_capture_fd, data, len);
}

__attribute__((constructor))
static void uterm_capture_init(void) {
    orig_write   = (fn_write)  dlsym(RTLD_NEXT, "write");
    orig_read    = (fn_read)   dlsym(RTLD_NEXT, "read");
    orig_connect = (fn_connect)dlsym(RTLD_NEXT, "connect");

    const char *path = getenv("UTERM_CAPTURE_SOCKET");
    if (!path || !*path) return;

    g_capture_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (g_capture_fd < 0) return;

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    if (orig_connect(g_capture_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(g_capture_fd);
        g_capture_fd = -1;
    }
}

ssize_t write(int fd, const void *buf, size_t count) {
    ssize_t ret = orig_write(fd, buf, count);
    if (ret > 0 && (fd == STDOUT_FILENO || fd == STDERR_FILENO)) {
        send_frame(CHANNEL_STDOUT, buf, (size_t)ret);
    }
    return ret;
}

ssize_t read(int fd, void *buf, size_t count) {
    ssize_t ret = orig_read(fd, buf, count);
    if (ret > 0 && fd == STDIN_FILENO) {
        send_frame(CHANNEL_STDIN, buf, (size_t)ret);
    }
    return ret;
}

int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    if (sockfd == g_capture_fd) return orig_connect(sockfd, addr, addrlen);
    int ret = orig_connect(sockfd, addr, addrlen);
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

#endif
