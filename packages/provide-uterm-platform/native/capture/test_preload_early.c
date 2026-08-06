/* packages/provide-uterm-platform/native/capture/test_preload_early.c
 * SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Regression: an executable preinit hook can call an interposed function
 * before libuterm_capture's constructor has resolved RTLD_NEXT. */

#include <stdlib.h>
#include <unistd.h>

static void early_write(void) {
    static const char message[] = "preinit write survived\n";
    if (write(STDOUT_FILENO, message, sizeof(message) - 1U) < 0) {
        _exit(90);
    }
}

__attribute__((section(".preinit_array"), used))
static void (*const early_write_hook)(void) = early_write;

int main(void) {
    return EXIT_SUCCESS;
}
