#!/bin/sh
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

set -eu

target=$1
platform=$2

if nm -u "$target" | grep -E '(__atomic|[ _](malloc|free|pthread_))' >/dev/null; then
    echo "FAIL $target has a heap, pthread, or out-of-line atomic dependency" >&2
    nm -u "$target" | grep -E '(__atomic|[ _](malloc|free|pthread_))' >&2
    exit 1
fi

if [ "$platform" = Darwin ]; then
    if nm -gU "$target" | grep -E '_capture_(socket|writer)' >/dev/null; then
        echo "FAIL $target exports an internal capture helper" >&2
        nm -gU "$target" | grep -E '_capture_(socket|writer)' >&2
        exit 1
    fi
else
    # The exported set is an allowlist, not a floor: anything extra is a symbol
    # this library would interpose in every preloaded process, so it has to be
    # deliberate. splice/sendfile/copy_file_range are hooked because kernel-space
    # copies issue no read/write and would otherwise be invisible; sendfile64 is
    # the same call under the name a _FILE_OFFSET_BITS=64 caller binds. tee and
    # pread are NOT exported — they are only called inward, to recover the bytes.
    actual=$(nm -D --defined-only "$target" | awk 'NF >= 3 {print $3}' | sort)
    expected=$(printf '%s\n' connect copy_file_range read sendfile sendfile64 splice write)
    if [ "$actual" != "$expected" ]; then
        echo "FAIL $target dynamic exports differ from the hooked-syscall allowlist" >&2
        printf 'actual:\n%s\nexpected:\n%s\n' "$actual" "$expected" >&2
        exit 1
    fi
fi

echo "PASS capture symbol visibility"
