/* SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

#include "capture_writer.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>

#define FRAME_HEADER_LEN 5U
#define FRAME_STACK_CAP 4096U
#define FRAME_PAYLOAD_CAP (FRAME_STACK_CAP - FRAME_HEADER_LEN)

_Static_assert(ATOMIC_INT_LOCK_FREE == 2,
               "capture hook counters require lock-free unsigned atomics");

int capture_socket_set_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) {
        return -1;
    }
    return fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static void increment(atomic_uint *counter) {
    (void)atomic_fetch_add_explicit(counter, 1U, memory_order_relaxed);
}

static enum capture_emit_result disable_writer(struct capture_writer *writer) {
    if (atomic_exchange_explicit(&writer->enabled, 0U, memory_order_acq_rel) != 0U) {
        increment(&writer->disabled_errors);
        writer->close_fn(writer->fd, writer->context);
    }
    return CAPTURE_EMIT_DISABLED;
}

static enum capture_emit_result preserve_errno(enum capture_emit_result result,
                                               int caller_errno) {
    errno = caller_errno;
    return result;
}

int capture_writer_init(struct capture_writer *writer, int fd, int send_flags,
                        capture_send_fn send_fn, capture_close_fn close_fn,
                        void *context) {
    if (writer == NULL || fd < 0 || send_fn == NULL || close_fn == NULL) {
        errno = EINVAL;
        return -1;
    }
    atomic_flag_clear_explicit(&writer->serialization, memory_order_relaxed);
    writer->send_fn = send_fn;
    writer->close_fn = close_fn;
    writer->context = context;
    writer->fd = fd;
    writer->send_flags = send_flags;
    atomic_init(&writer->enabled, 1U);
    atomic_init(&writer->frames_sent, 0U);
    atomic_init(&writer->dropped_busy, 0U);
    atomic_init(&writer->dropped_would_block, 0U);
    atomic_init(&writer->dropped_invalid, 0U);
    atomic_init(&writer->disabled_errors, 0U);
    return 0;
}

void capture_writer_destroy(struct capture_writer *writer) {
    (void)writer;
}

enum capture_emit_result capture_writer_emit(struct capture_writer *writer,
                                             uint8_t channel,
                                             const void *payload,
                                             size_t payload_len) {
    const int caller_errno = errno;
    uint8_t frame[FRAME_STACK_CAP];
    const uint8_t *payload_bytes = payload;
    size_t remaining = payload_len;
    size_t offset = 0U;
    ssize_t sent;
    int send_errno;

    if (atomic_load_explicit(&writer->enabled, memory_order_acquire) == 0U) {
        return preserve_errno(CAPTURE_EMIT_DISABLED, caller_errno);
    }
    if (payload_len > UINT32_MAX ||
        (payload_len != 0U && payload == NULL)) {
        increment(&writer->dropped_invalid);
        return preserve_errno(CAPTURE_EMIT_REJECTED, caller_errno);
    }

    if (atomic_flag_test_and_set_explicit(&writer->serialization,
                                          memory_order_acquire)) {
        increment(&writer->dropped_busy);
        return preserve_errno(CAPTURE_EMIT_DROPPED, caller_errno);
    }
    if (atomic_load_explicit(&writer->enabled, memory_order_acquire) == 0U) {
        atomic_flag_clear_explicit(&writer->serialization, memory_order_release);
        return preserve_errno(CAPTURE_EMIT_DISABLED, caller_errno);
    }

    do {
        size_t chunk_len = remaining;
        uint32_t encoded_len;
        size_t total;
        size_t index;

        if (chunk_len > FRAME_PAYLOAD_CAP) {
            chunk_len = FRAME_PAYLOAD_CAP;
        }
        encoded_len = (uint32_t)chunk_len;
        total = FRAME_HEADER_LEN + chunk_len;

        frame[0] = channel;
        frame[1] = (uint8_t)((encoded_len >> 24) & 0xffU);
        frame[2] = (uint8_t)((encoded_len >> 16) & 0xffU);
        frame[3] = (uint8_t)((encoded_len >> 8) & 0xffU);
        frame[4] = (uint8_t)(encoded_len & 0xffU);
        for (index = 0U; index < chunk_len; index++) {
            frame[FRAME_HEADER_LEN + index] = payload_bytes[offset + index];
        }

        do {
            sent = writer->send_fn(writer->fd, frame, total, writer->send_flags,
                                   writer->context);
            send_errno = errno;
        } while (sent < 0 && send_errno == EINTR);

        if (sent == (ssize_t)total) {
            increment(&writer->frames_sent);
            offset += chunk_len;
            remaining -= chunk_len;
            continue;
        }
        if (sent < 0 && (send_errno == EAGAIN || send_errno == EWOULDBLOCK)) {
            increment(&writer->dropped_would_block);
            atomic_flag_clear_explicit(&writer->serialization, memory_order_release);
            return preserve_errno(CAPTURE_EMIT_DROPPED, caller_errno);
        }

        (void)disable_writer(writer);
        atomic_flag_clear_explicit(&writer->serialization, memory_order_release);
        return preserve_errno(CAPTURE_EMIT_DISABLED, caller_errno);
    } while (remaining != 0U);

    atomic_flag_clear_explicit(&writer->serialization, memory_order_release);
    return preserve_errno(CAPTURE_EMIT_SENT, caller_errno);
}

int capture_writer_is_enabled(const struct capture_writer *writer) {
    return atomic_load_explicit(&writer->enabled, memory_order_acquire) != 0U;
}

void capture_writer_get_stats(const struct capture_writer *writer,
                              struct capture_writer_stats *stats) {
    stats->frames_sent = atomic_load_explicit(&writer->frames_sent, memory_order_relaxed);
    stats->dropped_busy = atomic_load_explicit(&writer->dropped_busy, memory_order_relaxed);
    stats->dropped_would_block = atomic_load_explicit(&writer->dropped_would_block,
                                                     memory_order_relaxed);
    stats->dropped_invalid = atomic_load_explicit(&writer->dropped_invalid,
                                                 memory_order_relaxed);
    stats->disabled_errors = atomic_load_explicit(&writer->disabled_errors,
                                                 memory_order_relaxed);
}
