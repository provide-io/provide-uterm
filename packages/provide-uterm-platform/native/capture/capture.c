/* packages/provide-uterm-platform/native/capture/capture.c
 * SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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
#include "capture_writer.h"

#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <signal.h>
#include <sys/un.h>
#include <sys/syscall.h>
#include <unistd.h>

#define CHANNEL_STDOUT  0x01
#define CHANNEL_STDIN   0x02
#define CHANNEL_CONNECT 0x03

#if defined(__GNUC__)
#define UTERM_EXPORT __attribute__((visibility("default")))
#else
#define UTERM_EXPORT
#endif

static atomic_int g_capture_fd = ATOMIC_VAR_INIT(-1);
static atomic_uint g_writer_ready = ATOMIC_VAR_INIT(0U);
static struct capture_writer g_writer = CAPTURE_WRITER_INITIALIZER;

_Static_assert(ATOMIC_INT_LOCK_FREE == 2,
               "capture hooks require lock-free integer atomics");

typedef ssize_t (*fn_write)(int, const void *, size_t);
typedef ssize_t (*fn_read)(int, void *, size_t);
typedef int     (*fn_connect)(int, const struct sockaddr *, socklen_t);

static ssize_t capture_socket_send(int fd, const void *buffer, size_t length,
                                   int flags, void *context) {
    (void)context;
    return send(fd, buffer, length, flags);
}

static void capture_socket_close(int fd, void *context) {
    (void)context;
    atomic_store_explicit(&g_capture_fd, -1, memory_order_release);
    (void)close(fd);
}

static int capture_socket_open(void) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);

    if (fd < 0) {
        return -1;
    }
    if (capture_socket_set_nonblocking(fd) < 0) {
        (void)close(fd);
        return -1;
    }
    return fd;
}

static int capture_writer_start(int fd) {
#ifdef __APPLE__
    const int send_flags = 0;
#else
    const int send_flags = MSG_NOSIGNAL;
#endif

    if (capture_writer_init(&g_writer, fd, send_flags, capture_socket_send,
                            capture_socket_close, NULL) < 0) {
        (void)close(fd);
        return -1;
    }
    atomic_store_explicit(&g_capture_fd, fd, memory_order_release);
    atomic_store_explicit(&g_writer_ready, 1, memory_order_release);
    return 0;
}

static void send_frame(uint8_t channel, const void *data, size_t len) {
    if (!atomic_load_explicit(&g_writer_ready, memory_order_acquire)) {
        return;
    }
    (void)capture_writer_emit(&g_writer, channel, data, len);
}

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

__attribute__((constructor))
static void uterm_capture_init(void) {
    /* Read originals from the interpose structs — these hold link-time addresses
     * set before dyld patched the call sites. */
    g_real_write   = (fn_write)  _itp_write.original;
    g_real_read    = (fn_read)   _itp_read.original;
    g_real_connect = (fn_connect)_itp_connect.original;

    const char *path = getenv("UTERM_CAPTURE_SOCKET");
    if (!path || !*path) return;

    int capture_fd = capture_socket_open();
    if (capture_fd < 0) return;

    /* Suppress SIGPIPE on write() to a closed capture socket — otherwise a
     * disconnected capture consumer would kill the *captured* process. macOS
     * has no MSG_NOSIGNAL for send(), so set the socket option (the Linux
     * backend uses send(..., MSG_NOSIGNAL) on send_frame instead). */
    int nosigpipe = 1;
    if (setsockopt(capture_fd, SOL_SOCKET, SO_NOSIGPIPE, &nosigpipe,
                   sizeof(nosigpipe)) < 0) {
        (void)close(capture_fd);
        return;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    if (g_real_connect(capture_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        (void)close(capture_fd);
        return;
    }
    (void)capture_writer_start(capture_fd);
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
    if (sockfd == atomic_load_explicit(&g_capture_fd, memory_order_acquire)) {
        return g_real_connect(sockfd, addr, addrlen);
    }
    int ret = g_real_connect(sockfd, addr, addrlen);
    int application_errno = errno;
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
    errno = application_errno;
    return ret;
}

#else  /* ── Linux LD_PRELOAD implementation ──────────────────────────────── */

/* splice()/tee() are Linux-only, so these hooks have no macOS counterpart. */
typedef ssize_t (*fn_splice)(int, loff_t *, int, loff_t *, size_t, unsigned int);
typedef ssize_t (*fn_tee)(int, int, size_t, unsigned int);

static fn_write   orig_write;
static fn_read    orig_read;
static fn_connect orig_connect;
static fn_splice  orig_splice;
static fn_tee     orig_tee;

static ssize_t call_write(int fd, const void *buf, size_t count) {
    if (orig_write != NULL) {
        return orig_write(fd, buf, count);
    }
    return syscall(SYS_write, fd, buf, count);
}

static ssize_t call_read(int fd, void *buf, size_t count) {
    if (orig_read != NULL) {
        return orig_read(fd, buf, count);
    }
    return syscall(SYS_read, fd, buf, count);
}

static int call_connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    if (orig_connect != NULL) {
        return orig_connect(sockfd, addr, addrlen);
    }
    return (int)syscall(SYS_connect, sockfd, addr, addrlen);
}

static ssize_t call_splice(int fd_in, loff_t *off_in, int fd_out, loff_t *off_out,
                           size_t len, unsigned int flags) {
    if (orig_splice != NULL) {
        return orig_splice(fd_in, off_in, fd_out, off_out, len, flags);
    }
    return (ssize_t)syscall(SYS_splice, fd_in, off_in, fd_out, off_out, len, flags);
}

static ssize_t call_tee(int fd_in, int fd_out, size_t len, unsigned int flags) {
    if (orig_tee != NULL) {
        return orig_tee(fd_in, fd_out, len, flags);
    }
    return (ssize_t)syscall(SYS_tee, fd_in, fd_out, len, flags);
}

__attribute__((constructor))
static void uterm_capture_init(void) {
    orig_write   = (fn_write)  dlsym(RTLD_NEXT, "write");
    orig_read    = (fn_read)   dlsym(RTLD_NEXT, "read");
    orig_connect = (fn_connect)dlsym(RTLD_NEXT, "connect");
    orig_splice  = (fn_splice) dlsym(RTLD_NEXT, "splice");
    orig_tee     = (fn_tee)    dlsym(RTLD_NEXT, "tee");

    const char *path = getenv("UTERM_CAPTURE_SOCKET");
    if (!path || !*path) return;

    int capture_fd = capture_socket_open();
    if (capture_fd < 0) return;

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    if (call_connect(capture_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        (void)close(capture_fd);
        return;
    }
    (void)capture_writer_start(capture_fd);
}

UTERM_EXPORT ssize_t write(int fd, const void *buf, size_t count) {
    ssize_t ret = call_write(fd, buf, count);
    if (ret > 0 && (fd == STDOUT_FILENO || fd == STDERR_FILENO)) {
        send_frame(CHANNEL_STDOUT, buf, (size_t)ret);
    }
    return ret;
}

UTERM_EXPORT ssize_t read(int fd, void *buf, size_t count) {
    ssize_t ret = call_read(fd, buf, count);
    if (ret > 0 && fd == STDIN_FILENO) {
        send_frame(CHANNEL_STDIN, buf, (size_t)ret);
    }
    return ret;
}

/* Emit up to `want` bytes read back from the peek pipe.  Chunked because a
 * single splice can move far more than one frame's worth. */
static void emit_peeked(int peek_fd, size_t want, int as_stdin, int as_stdout) {
    char buf[8192];
    while (want > 0) {
        size_t chunk = want < sizeof(buf) ? want : sizeof(buf);
        ssize_t got = call_read(peek_fd, buf, chunk);
        if (got <= 0) {
            return;
        }
        if (as_stdin) {
            send_frame(CHANNEL_STDIN, buf, (size_t)got);
        }
        if (as_stdout) {
            send_frame(CHANNEL_STDOUT, buf, (size_t)got);
        }
        want -= (size_t)got;
    }
}

/* splice() moves bytes between fds in kernel space, so a captured process using
 * it produces no read()/write() and would otherwise be invisible — uutils
 * coreutils' cat copies pipe-to-pipe this way and emitted nothing at all.
 *
 * Capture without touching the data path: tee() duplicates pipe contents WITHOUT
 * consuming them, so peek first, let the real splice run untouched, then read the
 * duplicate back.  tee needs fd_in to be a pipe; when it is not (a file source,
 * i.e. off_in != NULL) it fails and the splice is simply uncaptured, exactly as
 * before.  The caller's SPLICE_F_NONBLOCK is forwarded so a non-blocking splice
 * does not become a blocking tee. */
UTERM_EXPORT ssize_t splice(int fd_in, loff_t *off_in, int fd_out, loff_t *off_out,
                            size_t len, unsigned int flags) {
    const int as_stdin  = (fd_in == STDIN_FILENO);
    const int as_stdout = (fd_out == STDOUT_FILENO || fd_out == STDERR_FILENO);

    if ((!as_stdin && !as_stdout) ||
        !atomic_load_explicit(&g_writer_ready, memory_order_acquire)) {
        return call_splice(fd_in, off_in, fd_out, off_out, len, flags);
    }

    int peek[2];
    if (pipe2(peek, O_CLOEXEC) < 0) {
        return call_splice(fd_in, off_in, fd_out, off_out, len, flags);
    }

    /* Peek BEFORE the splice — afterwards the bytes are gone. */
    ssize_t peeked = call_tee(fd_in, peek[1], len, flags & SPLICE_F_NONBLOCK);
    ssize_t ret = call_splice(fd_in, off_in, fd_out, off_out, len, flags);
    int application_errno = errno;

    if (ret > 0 && peeked > 0) {
        /* The peek pipe holds at most its own capacity, and the splice may move a
         * different count than tee duplicated; the shorter of the two is the
         * prefix known to be identical. */
        size_t want = (size_t)ret < (size_t)peeked ? (size_t)ret : (size_t)peeked;
        emit_peeked(peek[0], want, as_stdin, as_stdout);
    }

    (void)close(peek[0]);
    (void)close(peek[1]);
    errno = application_errno;
    return ret;
}

UTERM_EXPORT int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    if (sockfd == atomic_load_explicit(&g_capture_fd, memory_order_acquire)) {
        return call_connect(sockfd, addr, addrlen);
    }
    int ret = call_connect(sockfd, addr, addrlen);
    int application_errno = errno;
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
    errno = application_errno;
    return ret;
}

#endif
