/* SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

#ifndef UTERM_CAPTURE_WRITER_H
#define UTERM_CAPTURE_WRITER_H

#include <pthread.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

typedef ssize_t (*capture_send_fn)(int fd, const void *buffer, size_t length,
                                   int flags, void *context);
typedef void (*capture_close_fn)(int fd, void *context);

enum capture_emit_result {
    CAPTURE_EMIT_SENT = 0,
    CAPTURE_EMIT_DROPPED,
    CAPTURE_EMIT_REJECTED,
    CAPTURE_EMIT_DISABLED
};

struct capture_writer_stats {
    uint64_t frames_sent;
    uint64_t dropped_busy;
    uint64_t dropped_would_block;
    uint64_t dropped_invalid;
    uint64_t dropped_no_memory;
    uint64_t disabled_errors;
};

struct capture_writer {
    pthread_mutex_t serialization;
    capture_send_fn send_fn;
    capture_close_fn close_fn;
    void *context;
    int fd;
    int send_flags;
    atomic_bool enabled;
    atomic_uint_fast64_t frames_sent;
    atomic_uint_fast64_t dropped_busy;
    atomic_uint_fast64_t dropped_would_block;
    atomic_uint_fast64_t dropped_invalid;
    atomic_uint_fast64_t dropped_no_memory;
    atomic_uint_fast64_t disabled_errors;
};

int capture_writer_init(struct capture_writer *writer, int fd, int send_flags,
                        capture_send_fn send_fn, capture_close_fn close_fn,
                        void *context);
int capture_socket_set_nonblocking(int fd);
void capture_writer_destroy(struct capture_writer *writer);
enum capture_emit_result capture_writer_emit(struct capture_writer *writer,
                                             uint8_t channel,
                                             const void *payload,
                                             size_t payload_len);
int capture_writer_is_enabled(const struct capture_writer *writer);
void capture_writer_get_stats(const struct capture_writer *writer,
                              struct capture_writer_stats *stats);

#endif
