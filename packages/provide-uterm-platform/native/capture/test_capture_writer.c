/* SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

#include "capture_writer.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define ARRAY_LEN(values) (sizeof(values) / sizeof((values)[0]))
#define RETURN_FULL_LENGTH ((ssize_t)-2)

struct send_step {
    ssize_t result;
    int error_number;
};

struct scripted_send {
    struct send_step steps[4];
    size_t step_count;
    size_t calls;
    uint8_t bytes[64];
    size_t length;
    int observed_fd;
    int observed_flags;
    int closes;
};

static void fail(const char *test, const char *expression, int line) {
    fprintf(stderr, "FAIL %s:%d: %s\n", test, line, expression);
    exit(EXIT_FAILURE);
}

#define CHECK(test, expression) do { \
    if (!(expression)) fail((test), #expression, __LINE__); \
} while (0)

static ssize_t scripted_send_callback(int fd, const void *buffer, size_t length,
                                      int flags, void *context) {
    struct scripted_send *script = context;
    size_t step_index = script->calls;
    script->calls++;
    script->observed_fd = fd;
    script->observed_flags = flags;
    script->length = length;
    if (length <= sizeof(script->bytes)) {
        memcpy(script->bytes, buffer, length);
    }
    if (step_index >= script->step_count) {
        return (ssize_t)length;
    }
    errno = script->steps[step_index].error_number;
    if (script->steps[step_index].result == RETURN_FULL_LENGTH) {
        return (ssize_t)length;
    }
    return script->steps[step_index].result;
}

static void scripted_close_callback(int fd, void *context) {
    struct scripted_send *script = context;
    (void)fd;
    script->closes++;
}

static void init_writer(const char *test, struct capture_writer *writer,
                        struct scripted_send *script) {
    CHECK(test, capture_writer_init(writer, 17, 23, scripted_send_callback,
                                    scripted_close_callback, script) == 0);
}

static void test_exact_wire_bytes(void) {
    const char *test = "exact wire bytes";
    struct scripted_send script = {0};
    struct capture_writer writer;
    const uint8_t expected[] = {0x03, 0x00, 0x00, 0x00, 0x03, 'a', 'b', 'c'};
    struct capture_writer_stats stats;

    init_writer(test, &writer, &script);
    CHECK(test, capture_writer_emit(&writer, 0x03, "abc", 3) == CAPTURE_EMIT_SENT);
    CHECK(test, script.calls == 1);
    CHECK(test, script.observed_fd == 17);
    CHECK(test, script.observed_flags == 23);
    CHECK(test, script.length == sizeof(expected));
    CHECK(test, memcmp(script.bytes, expected, sizeof(expected)) == 0);
    capture_writer_get_stats(&writer, &stats);
    CHECK(test, stats.frames_sent == 1);
    capture_writer_destroy(&writer);
}

static void test_capture_socket_is_made_nonblocking(void) {
    const char *test = "nonblocking socket";
    int sockets[2];
    int flags;

    CHECK(test, socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
    CHECK(test, capture_socket_set_nonblocking(sockets[0]) == 0);
    flags = fcntl(sockets[0], F_GETFL, 0);
    CHECK(test, flags >= 0);
    CHECK(test, (flags & O_NONBLOCK) != 0);
    CHECK(test, close(sockets[0]) == 0);
    CHECK(test, close(sockets[1]) == 0);
}

static void test_oversize_rejected_before_payload_access(void) {
    const char *test = "oversize rejection";
    struct scripted_send script = {0};
    struct capture_writer writer;
    struct capture_writer_stats stats;

    init_writer(test, &writer, &script);
#if SIZE_MAX > UINT32_MAX
    CHECK(test, capture_writer_emit(&writer, 1, (const void *)(uintptr_t)1,
                                    (size_t)UINT32_MAX + 1U) == CAPTURE_EMIT_REJECTED);
#else
    CHECK(test, capture_writer_emit(&writer, 1, (const void *)(uintptr_t)1,
                                    SIZE_MAX) == CAPTURE_EMIT_REJECTED);
#endif
    CHECK(test, script.calls == 0);
    capture_writer_get_stats(&writer, &stats);
    CHECK(test, stats.dropped_invalid == 1);
    capture_writer_destroy(&writer);
}

static void test_eagain_drops_without_disabling(void) {
    const char *test = "EAGAIN drop";
    struct scripted_send script = {
        .steps = {{-1, EAGAIN}},
        .step_count = 1,
    };
    struct capture_writer writer;
    struct capture_writer_stats stats;

    init_writer(test, &writer, &script);
    CHECK(test, capture_writer_emit(&writer, 1, "x", 1) == CAPTURE_EMIT_DROPPED);
    CHECK(test, capture_writer_is_enabled(&writer));
    CHECK(test, script.closes == 0);
    capture_writer_get_stats(&writer, &stats);
    CHECK(test, stats.dropped_would_block == 1);
    capture_writer_destroy(&writer);
}

static void test_emit_preserves_observed_process_errno(void) {
    const char *test = "errno preservation";
    struct scripted_send script = {
        .steps = {{-1, EAGAIN}},
        .step_count = 1,
    };
    struct capture_writer writer;

    init_writer(test, &writer, &script);
    errno = EDOM;
    CHECK(test, capture_writer_emit(&writer, 1, "x", 1) == CAPTURE_EMIT_DROPPED);
    CHECK(test, errno == EDOM);
    capture_writer_destroy(&writer);
}

static void test_eintr_retries_before_progress(void) {
    const char *test = "EINTR retry";
    struct scripted_send script = {
        .steps = {{-1, EINTR}, {RETURN_FULL_LENGTH, 0}},
        .step_count = 2,
    };
    struct capture_writer writer;

    init_writer(test, &writer, &script);
    CHECK(test, capture_writer_emit(&writer, 2, "ok", 2) == CAPTURE_EMIT_SENT);
    CHECK(test, script.calls == 2);
    CHECK(test, script.closes == 0);
    capture_writer_destroy(&writer);
}

static void test_positive_short_write_disables_without_retry(void) {
    const char *test = "short write disable";
    struct scripted_send script = {
        .steps = {{1, 0}},
        .step_count = 1,
    };
    struct capture_writer writer;
    struct capture_writer_stats stats;

    init_writer(test, &writer, &script);
    CHECK(test, capture_writer_emit(&writer, 1, "payload", 7) == CAPTURE_EMIT_DISABLED);
    CHECK(test, script.calls == 1);
    CHECK(test, script.closes == 1);
    CHECK(test, !capture_writer_is_enabled(&writer));
    CHECK(test, capture_writer_emit(&writer, 1, "later", 5) == CAPTURE_EMIT_DISABLED);
    CHECK(test, script.calls == 1);
    capture_writer_get_stats(&writer, &stats);
    CHECK(test, stats.disabled_errors == 1);
    capture_writer_destroy(&writer);
}

static void test_fatal_error_disables_and_stays_disabled(void) {
    const char *test = "fatal disable";
    struct scripted_send script = {
        .steps = {{-1, EPIPE}},
        .step_count = 1,
    };
    struct capture_writer writer;

    init_writer(test, &writer, &script);
    CHECK(test, capture_writer_emit(&writer, 1, "x", 1) == CAPTURE_EMIT_DISABLED);
    CHECK(test, script.closes == 1);
    CHECK(test, capture_writer_emit(&writer, 1, "y", 1) == CAPTURE_EMIT_DISABLED);
    CHECK(test, script.calls == 1);
    capture_writer_destroy(&writer);
}

struct blocking_send {
    pthread_mutex_t mutex;
    pthread_cond_t condition;
    int entered;
    int release;
    int calls;
};

struct producer {
    struct capture_writer *writer;
    enum capture_emit_result result;
};

static ssize_t blocking_send_callback(int fd, const void *buffer, size_t length,
                                      int flags, void *context) {
    struct blocking_send *blocking = context;
    (void)fd;
    (void)buffer;
    (void)flags;
    pthread_mutex_lock(&blocking->mutex);
    blocking->calls++;
    blocking->entered = 1;
    pthread_cond_broadcast(&blocking->condition);
    while (!blocking->release) {
        pthread_cond_wait(&blocking->condition, &blocking->mutex);
    }
    pthread_mutex_unlock(&blocking->mutex);
    return (ssize_t)length;
}

static void noop_close_callback(int fd, void *context) {
    (void)fd;
    (void)context;
}

static void *producer_main(void *argument) {
    struct producer *producer = argument;
    producer->result = capture_writer_emit(producer->writer, 1, "first", 5);
    return NULL;
}

static void test_busy_serialization_drops_second_producer(void) {
    const char *test = "busy serialization drop";
    struct blocking_send blocking = {
        .mutex = PTHREAD_MUTEX_INITIALIZER,
        .condition = PTHREAD_COND_INITIALIZER,
    };
    struct capture_writer writer;
    struct producer producer = {.writer = &writer};
    pthread_t thread;
    struct capture_writer_stats stats;

    CHECK(test, capture_writer_init(&writer, 17, 0, blocking_send_callback,
                                    noop_close_callback, &blocking) == 0);
    CHECK(test, pthread_create(&thread, NULL, producer_main, &producer) == 0);
    pthread_mutex_lock(&blocking.mutex);
    while (!blocking.entered) {
        pthread_cond_wait(&blocking.condition, &blocking.mutex);
    }
    pthread_mutex_unlock(&blocking.mutex);

    CHECK(test, capture_writer_emit(&writer, 2, "second", 6) == CAPTURE_EMIT_DROPPED);
    capture_writer_get_stats(&writer, &stats);
    CHECK(test, stats.dropped_busy == 1);

    pthread_mutex_lock(&blocking.mutex);
    blocking.release = 1;
    pthread_cond_broadcast(&blocking.condition);
    pthread_mutex_unlock(&blocking.mutex);
    CHECK(test, pthread_join(thread, NULL) == 0);
    CHECK(test, producer.result == CAPTURE_EMIT_SENT);
    CHECK(test, blocking.calls == 1);

    capture_writer_destroy(&writer);
    pthread_cond_destroy(&blocking.condition);
    pthread_mutex_destroy(&blocking.mutex);
}

int main(void) {
    test_exact_wire_bytes();
    test_capture_socket_is_made_nonblocking();
    test_oversize_rejected_before_payload_access();
    test_eagain_drops_without_disabling();
    test_emit_preserves_observed_process_errno();
    test_eintr_retries_before_progress();
    test_positive_short_write_disables_without_retry();
    test_fatal_error_disables_and_stays_disabled();
    test_busy_serialization_drops_second_producer();
    printf("PASS capture_writer self-tests (%zu cases)\n", (size_t)9);
    return EXIT_SUCCESS;
}
