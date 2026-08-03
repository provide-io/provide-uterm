# CM-04: Length-Aware Native Socket Address Formatting

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the LD_PRELOAD capture library reading past the caller-supplied
address length when formatting a connected socket address, so log output cannot
contain adjacent process memory.

**Architecture:** Both platform branches of `capture.c` format socket addresses
inline and identically, and both ignore the `addrlen` the hook receives. Extract
one shared formatter into `capture_writer.c` — the module that already has a
unit-test target — and call it from both hooks. The formatter takes the length,
never scans past it, and distinguishes pathname, abstract, unnamed, and
truncated Unix addresses.

**Tech Stack:** C11, `-Wall -Wextra -Werror`, hand-rolled test harness in
`test_capture_writer.c` (`CHECK(name, expr)` macro), GNU Make.

## Global Constraints

- Language: C11 (`-std=c11`), compiled with `-Wall -Wextra -Werror`. Any warning
  fails the build.
- All new files carry SPDX headers, in the form used by every other file in
  this directory:

```c
/* SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
```

- Internal symbols use the `CAPTURE_INTERNAL` visibility macro from
  `capture_writer.h`. The library is built `-fvisibility=hidden`; only the
  interposed libc symbols are exported, and `test_symbols.sh` enforces that.
- No dynamic allocation in the formatter. It writes into a caller-owned buffer.
- Reentrant libc only. `inet_ntoa` is banned — it returns a pointer to a static
  buffer and the hook runs on arbitrary application threads.
- Working directory for all commands: `packages/provide-uterm-platform/native/capture/`.

## Context

`capture.c` hooks `connect(2)` twice — once for the macOS `DYLD_INTERPOSE`
branch (`capture.c:198`) and once for the Linux `LD_PRELOAD` branch
(`capture.c:271`). Both bodies contain the same formatting block, and both
contain the same defect:

```c
} else if (addr->sa_family == AF_UNIX) {
    const struct sockaddr_un *un = (const struct sockaddr_un *)addr;
    snprintf(addrstr, sizeof(addrstr), "unix:%s", un->sun_path);
}
```

`%s` scans to a NUL terminator. The kernel does not require `sun_path` to be
terminated, and `addrlen` may describe far fewer bytes than
`sizeof(un->sun_path)` (108 on Linux, 104 on macOS). A caller passing a short
`addrlen` with an unterminated `sun_path` causes the formatter to read whatever
follows the caller's `struct sockaddr_un` in memory and emit it on the capture
channel.

Both branches also call `inet_ntoa` (`capture.c:209`, `capture.c:282`), which is
not reentrant.

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-04.

## File Structure

- `capture_writer.h` — add the `capture_format_sockaddr` declaration. This
  header is already the shared-internals header both `capture.c` and the test
  binary include, so the formatter becomes testable without loading the shared
  library or interposing anything.
- `capture_writer.c` — add the implementation. Placed here rather than in
  `capture.c` because `capture.c` is compiled only into the shared library and
  is not linked into any test target.
- `test_capture_addr.c` — new test binary for the formatter. Kept separate from
  `test_capture_writer.c` because that file is already 400+ lines covering frame
  emission, and address formatting is an unrelated responsibility.
- `Makefile` — add the new test target and wire it into `make test`.
- `capture.c` — replace both inline formatting blocks with a call.

---

### Task 1: Shared formatter handles pathname and unnamed Unix addresses

**Files:**
- Modify: `packages/provide-uterm-platform/native/capture/capture_writer.h`
- Modify: `packages/provide-uterm-platform/native/capture/capture_writer.c`
- Create: `packages/provide-uterm-platform/native/capture/test_capture_addr.c`
- Modify: `packages/provide-uterm-platform/native/capture/Makefile`

**Interfaces:**
- Consumes: `CAPTURE_INTERNAL` from `capture_writer.h`.
- Produces:
  ```c
  CAPTURE_INTERNAL size_t capture_format_sockaddr(const struct sockaddr *addr,
                                                  socklen_t addrlen,
                                                  char *out, size_t out_len);
  ```
  Returns the number of bytes written to `out`, not counting the NUL. Returns 0
  and writes `out[0] = '\0'` when the address cannot be formatted (null pointer,
  zero-length output buffer, or an address family the capture channel does not
  describe). Tasks 2 and 3 extend this same function; Task 4 calls it.

- [ ] **Step 1: Write the failing test**

Create `test_capture_addr.c`:

```c
/* SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

#include "capture_writer.h"

#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>

static void fail(const char *test, const char *expression, int line) {
    fprintf(stderr, "FAIL %s:%d: %s\n", test, line, expression);
    exit(EXIT_FAILURE);
}

#define CHECK(test, expression) do { \
    if (!(expression)) fail((test), #expression, __LINE__); \
} while (0)

/* Build a sockaddr_un holding `path`, and return the addrlen a caller would
 * pass for it: the offset of sun_path plus the path bytes plus its NUL. */
static socklen_t make_unix(struct sockaddr_un *un, const char *path) {
    memset(un, 0, sizeof(*un));
    un->sun_family = AF_UNIX;
    const size_t len = strlen(path);
    memcpy(un->sun_path, path, len);
    return (socklen_t)(offsetof(struct sockaddr_un, sun_path) + len + 1);
}

static void test_pathname_address_is_formatted(void) {
    struct sockaddr_un un;
    const socklen_t addrlen = make_unix(&un, "/tmp/uterm.sock");
    char out[256];

    const size_t written =
        capture_format_sockaddr((const struct sockaddr *)&un, addrlen, out, sizeof(out));

    CHECK("pathname", strcmp(out, "unix:/tmp/uterm.sock") == 0);
    CHECK("pathname", written == strlen("unix:/tmp/uterm.sock"));
}

static void test_unnamed_address_is_labelled(void) {
    struct sockaddr_un un;
    memset(&un, 0, sizeof(un));
    un.sun_family = AF_UNIX;
    char out[256];

    /* A socket that was never bound: addrlen covers the family and nothing else. */
    const size_t written = capture_format_sockaddr(
        (const struct sockaddr *)&un,
        (socklen_t)offsetof(struct sockaddr_un, sun_path),
        out, sizeof(out));

    CHECK("unnamed", strcmp(out, "unix:unnamed") == 0);
    CHECK("unnamed", written == strlen("unix:unnamed"));
}

static void test_null_address_writes_empty_string(void) {
    char out[8];
    out[0] = 'x';

    const size_t written = capture_format_sockaddr(NULL, 0, out, sizeof(out));

    CHECK("null", written == 0);
    CHECK("null", out[0] == '\0');
}

int main(void) {
    test_pathname_address_is_formatted();
    test_unnamed_address_is_labelled();
    test_null_address_writes_empty_string();
    printf("test_capture_addr: all tests passed\n");
    return EXIT_SUCCESS;
}
```

- [ ] **Step 2: Add the test target to the Makefile**

In `Makefile`, add `ADDR_TEST_TARGET` beside the existing `TEST_TARGET`
definition:

```make
ADDR_TEST_TARGET = test_capture_addr
```

Add the build rule after the existing `$(TEST_TARGET)` rule:

```make
$(ADDR_TEST_TARGET): test_capture_addr.c capture_writer.c capture_writer.h
	cc $(TEST_CFLAGS) -o $(ADDR_TEST_TARGET) test_capture_addr.c capture_writer.c
```

Extend the `test` target to build and run it:

```make
test: $(TEST_TARGET) $(ADDR_TEST_TARGET) $(TARGET)
	./$(TEST_TARGET)
	./$(ADDR_TEST_TARGET)
	./$(SYMBOL_TEST) $(TARGET) $(UNAME)
```

Add it to `clean`:

```make
clean:
	rm -f $(TARGET) $(TEST_TARGET) $(ADDR_TEST_TARGET)
	rm -rf $(TARGET).dSYM $(TEST_TARGET).dSYM $(ADDR_TEST_TARGET).dSYM
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd packages/provide-uterm-platform/native/capture && make test_capture_addr`

Expected: FAIL at compile time with an implicit-declaration error for
`capture_format_sockaddr`. Under `-Werror` this is a hard error, so the binary
is never produced.

- [ ] **Step 4: Declare the function**

In `capture_writer.h`, add `#include <sys/socket.h>` to the existing include
block (it already includes `<sys/types.h>`, but `socklen_t` and `struct
sockaddr` come from `<sys/socket.h>`), then add the declaration after
`capture_writer_get_stats`:

```c
CAPTURE_INTERNAL size_t capture_format_sockaddr(const struct sockaddr *addr,
                                                socklen_t addrlen,
                                                char *out, size_t out_len);
```

- [ ] **Step 5: Write the minimal implementation**

In `capture_writer.c`, add these includes to the existing include block:

```c
#include <stddef.h>
#include <sys/un.h>
```

Then add the implementation:

```c
/* Format a connected peer address for the capture channel.
 *
 * Everything here is driven by `addrlen` rather than by NUL scanning. The
 * kernel does not promise sun_path is terminated, and a caller may pass an
 * addrlen far shorter than the struct, so `%s` would read whatever follows the
 * caller's struct in memory and emit it on the wire.
 */
CAPTURE_INTERNAL size_t capture_format_sockaddr(const struct sockaddr *addr,
                                                socklen_t addrlen,
                                                char *out, size_t out_len) {
    if (out == NULL || out_len == 0) {
        return 0;
    }
    out[0] = '\0';
    if (addr == NULL) {
        return 0;
    }

    if (addr->sa_family == AF_UNIX) {
        const size_t path_offset = offsetof(struct sockaddr_un, sun_path);
        if ((size_t)addrlen <= path_offset) {
            /* Nothing but the family arrived: an unbound socket. */
            const int n = snprintf(out, out_len, "unix:unnamed");
            return (n > 0 && (size_t)n < out_len) ? (size_t)n : 0;
        }

        const struct sockaddr_un *un = (const struct sockaddr_un *)addr;
        size_t avail = (size_t)addrlen - path_offset;
        if (avail > sizeof(un->sun_path)) {
            avail = sizeof(un->sun_path);
        }

        /* strnlen, not strlen: the bound is what makes this safe. */
        const size_t path_len = strnlen(un->sun_path, avail);
        const int n = snprintf(out, out_len, "unix:%.*s", (int)path_len, un->sun_path);
        return (n > 0 && (size_t)n < out_len) ? (size_t)n : 0;
    }

    return 0;
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd packages/provide-uterm-platform/native/capture && make test_capture_addr && ./test_capture_addr`

Expected: `test_capture_addr: all tests passed`

- [ ] **Step 7: Commit**

```bash
git add packages/provide-uterm-platform/native/capture/capture_writer.h \
        packages/provide-uterm-platform/native/capture/capture_writer.c \
        packages/provide-uterm-platform/native/capture/test_capture_addr.c \
        packages/provide-uterm-platform/native/capture/Makefile
git commit -m "feat(native): bound Unix socket address formatting by addrlen

The connect hook formatted sun_path with %s, which scans to a NUL the
kernel never promised is there. A caller passing a short addrlen with an
unterminated path made the hook read past the struct and emit adjacent
memory on the capture channel.

Format from the supplied length instead: derive the available byte count
from addrlen and offsetof, clamp it to the field, and bound the read with
strnlen. Lives in capture_writer.c so it is reachable from a test binary
without interposing anything."
```

---

### Task 2: Truncated and abstract Unix addresses

**Files:**
- Modify: `packages/provide-uterm-platform/native/capture/capture_writer.c`
- Modify: `packages/provide-uterm-platform/native/capture/test_capture_addr.c`

**Interfaces:**
- Consumes: `capture_format_sockaddr` from Task 1.
- Produces: no new symbols. Extends the same function's behavior.

- [ ] **Step 1: Write the failing tests**

Add to `test_capture_addr.c`, before `main`:

```c
static void test_unterminated_path_stops_at_addrlen(void) {
    /* The hostile shape: sun_path filled with no NUL anywhere, and an addrlen
     * describing only the first few bytes. Anything that scans for a
     * terminator walks off the end of the struct. */
    struct sockaddr_un un;
    memset(&un, 0xAB, sizeof(un));
    un.sun_family = AF_UNIX;
    memcpy(un.sun_path, "/tmp/abc", 8);
    char out[256];

    const size_t written = capture_format_sockaddr(
        (const struct sockaddr *)&un,
        (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 8),
        out, sizeof(out));

    CHECK("unterminated", strcmp(out, "unix:/tmp/abc") == 0);
    CHECK("unterminated", written == strlen("unix:/tmp/abc"));
}

static void test_addrlen_longer_than_struct_is_clamped(void) {
    struct sockaddr_un un;
    memset(&un, 0, sizeof(un));
    un.sun_family = AF_UNIX;
    memset(un.sun_path, 'A', sizeof(un.sun_path));
    char out[256];

    /* A caller claiming more bytes than the field holds must not widen the read. */
    const size_t written = capture_format_sockaddr(
        (const struct sockaddr *)&un, (socklen_t)(sizeof(un) + 4096), out, sizeof(out));

    CHECK("clamp", written == strlen("unix:") + sizeof(un.sun_path));
    CHECK("clamp", strncmp(out, "unix:AAAA", 9) == 0);
}

static void test_abstract_address_is_marked(void) {
    /* Linux abstract sockets: sun_path[0] is NUL and the name follows it.
     * Formatting from sun_path directly would render these as empty. */
    struct sockaddr_un un;
    memset(&un, 0, sizeof(un));
    un.sun_family = AF_UNIX;
    un.sun_path[0] = '\0';
    memcpy(un.sun_path + 1, "uterm", 5);
    char out[256];

    const size_t written = capture_format_sockaddr(
        (const struct sockaddr *)&un,
        (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 6),
        out, sizeof(out));

    CHECK("abstract", strcmp(out, "unix:@uterm") == 0);
    CHECK("abstract", written == strlen("unix:@uterm"));
}

static void test_nonprintable_bytes_are_escaped(void) {
    struct sockaddr_un un;
    const socklen_t addrlen = make_unix(&un, "/tmp/x");
    un.sun_path[5] = '\n';
    char out[256];

    capture_format_sockaddr((const struct sockaddr *)&un, addrlen, out, sizeof(out));

    CHECK("escape", strcmp(out, "unix:/tmp/\\x0a") == 0);
}
```

Register them in `main`, before the `printf`:

```c
    test_unterminated_path_stops_at_addrlen();
    test_addrlen_longer_than_struct_is_clamped();
    test_abstract_address_is_marked();
    test_nonprintable_bytes_are_escaped();
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd packages/provide-uterm-platform/native/capture && make test_capture_addr && ./test_capture_addr`

Expected: FAIL. `test_abstract_address_is_marked` fails first — the Task 1
implementation renders an abstract address as `unix:` because `strnlen` stops at
the leading NUL. `test_nonprintable_bytes_are_escaped` fails because nothing
escapes yet. The two truncation tests pass already; they are regression cover
for Task 1's bound, and they must keep passing.

- [ ] **Step 3: Implement escaping and the abstract/unnamed split**

Replace the `AF_UNIX` branch body in `capture_writer.c` with:

```c
    if (addr->sa_family == AF_UNIX) {
        const size_t path_offset = offsetof(struct sockaddr_un, sun_path);
        if ((size_t)addrlen <= path_offset) {
            return capture_copy_literal(out, out_len, "unix:unnamed");
        }

        const struct sockaddr_un *un = (const struct sockaddr_un *)addr;
        size_t avail = (size_t)addrlen - path_offset;
        if (avail > sizeof(un->sun_path)) {
            avail = sizeof(un->sun_path);
        }

        /* Abstract sockets (Linux) put a NUL first and the name after it.
         * Rendering from sun_path directly would show every one of them as
         * empty, so the leading NUL becomes a visible '@'. */
        const int abstract = (avail > 1 && un->sun_path[0] == '\0');
        const char *bytes = abstract ? un->sun_path + 1 : un->sun_path;
        const size_t span = abstract ? avail - 1 : avail;
        const size_t used = abstract ? span : strnlen(bytes, span);
        if (used == 0 && !abstract) {
            return capture_copy_literal(out, out_len, "unix:unnamed");
        }

        return capture_escape_into(out, out_len, abstract ? "unix:@" : "unix:", bytes, used);
    }
```

Add both helpers above `capture_format_sockaddr`:

```c
/* Copy a NUL-terminated literal, reporting how much landed. */
static size_t capture_copy_literal(char *out, size_t out_len, const char *literal) {
    const int n = snprintf(out, out_len, "%s", literal);
    return (n > 0 && (size_t)n < out_len) ? (size_t)n : 0;
}

/* Append `count` bytes of `bytes` to `prefix`, rendering anything outside
 * printable ASCII as \xNN. A socket path is attacker-influenced text that ends
 * up in a log, so control bytes must not travel intact. Stops early rather than
 * truncating mid-escape. */
static size_t capture_escape_into(char *out, size_t out_len, const char *prefix,
                                  const char *bytes, size_t count) {
    size_t at = 0;
    for (const char *p = prefix; *p != '\0'; p++) {
        if (at + 1 >= out_len) {
            out[at] = '\0';
            return at;
        }
        out[at++] = *p;
    }

    for (size_t i = 0; i < count; i++) {
        const unsigned char c = (unsigned char)bytes[i];
        if (c >= 0x20 && c < 0x7F) {
            if (at + 1 >= out_len) break;
            out[at++] = (char)c;
        } else {
            if (at + 4 >= out_len) break;
            at += (size_t)snprintf(out + at, out_len - at, "\\x%02x", c);
        }
    }

    out[at] = '\0';
    return at;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd packages/provide-uterm-platform/native/capture && make test_capture_addr && ./test_capture_addr`

Expected: `test_capture_addr: all tests passed`

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-platform/native/capture/capture_writer.c \
        packages/provide-uterm-platform/native/capture/test_capture_addr.c
git commit -m "feat(native): distinguish abstract, unnamed and truncated Unix addresses

An abstract socket puts a NUL first and its name after it, so bounding
the read with strnlen alone rendered every one of them as an empty path.
Split the cases explicitly and mark abstract names with '@'.

Escape anything outside printable ASCII as \\xNN on the way out. A socket
path is attacker-influenced text headed for a log; control bytes should
not travel intact."
```

---

### Task 3: Reentrant IPv4 and IPv6 formatting

**Files:**
- Modify: `packages/provide-uterm-platform/native/capture/capture_writer.c`
- Modify: `packages/provide-uterm-platform/native/capture/test_capture_addr.c`

**Interfaces:**
- Consumes: `capture_format_sockaddr`, `capture_copy_literal`,
  `capture_escape_into` from Tasks 1 and 2.
- Produces: no new symbols.

- [ ] **Step 1: Write the failing tests**

Add to `test_capture_addr.c` (and add `#include <arpa/inet.h>` and
`#include <netinet/in.h>` to its includes):

```c
static void test_ipv4_address_is_formatted(void) {
    struct sockaddr_in in4;
    memset(&in4, 0, sizeof(in4));
    in4.sin_family = AF_INET;
    in4.sin_port = htons(8080);
    CHECK("ipv4", inet_pton(AF_INET, "192.0.2.10", &in4.sin_addr) == 1);
    char out[256];

    capture_format_sockaddr((const struct sockaddr *)&in4, sizeof(in4), out, sizeof(out));

    CHECK("ipv4", strcmp(out, "192.0.2.10:8080") == 0);
}

static void test_ipv6_address_is_bracketed(void) {
    struct sockaddr_in6 in6;
    memset(&in6, 0, sizeof(in6));
    in6.sin6_family = AF_INET6;
    in6.sin6_port = htons(443);
    CHECK("ipv6", inet_pton(AF_INET6, "2001:db8::1", &in6.sin6_addr) == 1);
    char out[256];

    capture_format_sockaddr((const struct sockaddr *)&in6, sizeof(in6), out, sizeof(out));

    CHECK("ipv6", strcmp(out, "[2001:db8::1]:443") == 0);
}

static void test_short_inet_addrlen_is_refused(void) {
    /* An addrlen too small to cover the struct means the fields are not all
     * there; reading them would be reading past what the caller supplied. */
    struct sockaddr_in in4;
    memset(&in4, 0, sizeof(in4));
    in4.sin_family = AF_INET;
    char out[256];
    out[0] = 'x';

    const size_t written = capture_format_sockaddr(
        (const struct sockaddr *)&in4, (socklen_t)(sizeof(in4) - 1), out, sizeof(out));

    CHECK("short-inet", written == 0);
    CHECK("short-inet", out[0] == '\0');
}

static void test_unknown_family_writes_nothing(void) {
    struct sockaddr addr;
    memset(&addr, 0, sizeof(addr));
    addr.sa_family = AF_UNSPEC;
    char out[256];
    out[0] = 'x';

    const size_t written =
        capture_format_sockaddr(&addr, sizeof(addr), out, sizeof(out));

    CHECK("unknown", written == 0);
    CHECK("unknown", out[0] == '\0');
}
```

Register all four in `main`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd packages/provide-uterm-platform/native/capture && make test_capture_addr && ./test_capture_addr`

Expected: FAIL at `test_ipv4_address_is_formatted` — the function returns 0 for
`AF_INET` because only `AF_UNIX` is handled so far.

- [ ] **Step 3: Implement the INET branches**

Add `#include <arpa/inet.h>` and `#include <netinet/in.h>` to
`capture_writer.c`. Insert these branches in `capture_format_sockaddr` before
the closing `return 0;`:

```c
    if (addr->sa_family == AF_INET) {
        if ((size_t)addrlen < sizeof(struct sockaddr_in)) {
            return 0;
        }
        const struct sockaddr_in *in4 = (const struct sockaddr_in *)addr;
        /* inet_ntop, not inet_ntoa: the hook runs on arbitrary application
         * threads and inet_ntoa hands back a shared static buffer. */
        char ip[INET_ADDRSTRLEN];
        if (inet_ntop(AF_INET, &in4->sin_addr, ip, sizeof(ip)) == NULL) {
            return 0;
        }
        const int n = snprintf(out, out_len, "%s:%d", ip, ntohs(in4->sin_port));
        return (n > 0 && (size_t)n < out_len) ? (size_t)n : 0;
    }

    if (addr->sa_family == AF_INET6) {
        if ((size_t)addrlen < sizeof(struct sockaddr_in6)) {
            return 0;
        }
        const struct sockaddr_in6 *in6 = (const struct sockaddr_in6 *)addr;
        char ip[INET6_ADDRSTRLEN];
        if (inet_ntop(AF_INET6, &in6->sin6_addr, ip, sizeof(ip)) == NULL) {
            return 0;
        }
        const int n = snprintf(out, out_len, "[%s]:%d", ip, ntohs(in6->sin6_port));
        return (n > 0 && (size_t)n < out_len) ? (size_t)n : 0;
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd packages/provide-uterm-platform/native/capture && make test_capture_addr && ./test_capture_addr`

Expected: `test_capture_addr: all tests passed`

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-platform/native/capture/capture_writer.c \
        packages/provide-uterm-platform/native/capture/test_capture_addr.c
git commit -m "feat(native): format IPv4 and IPv6 peers reentrantly and length-checked

inet_ntoa returns a pointer to a shared static buffer, and the connect
hook runs on whatever thread the application calls it from. Use
inet_ntop into a caller-owned buffer for both families.

Refuse an addrlen too small to cover the struct rather than reading
fields the caller did not supply."
```

---

### Task 4: Both hooks call the shared formatter

**Files:**
- Modify: `packages/provide-uterm-platform/native/capture/capture.c:198-222` (macOS branch)
- Modify: `packages/provide-uterm-platform/native/capture/capture.c:271-295` (Linux branch)

**Interfaces:**
- Consumes: `capture_format_sockaddr` from Tasks 1-3.
- Produces: no new symbols. Deletes two duplicated formatting blocks.

Line numbers are as measured on 2026-08-03 at `3574183e`; locate the blocks by
searching for `sun_path` if they have moved.

- [ ] **Step 1: Verify the current behavior is reachable**

Run: `cd packages/provide-uterm-platform/native/capture && grep -n "sun_path\|inet_ntoa" capture.c`

Expected: four hits — `capture.c:209` and `capture.c:282` (`inet_ntoa`),
`capture.c:217` and `capture.c:290` (`sun_path`). These are the two blocks this
task deletes. The hits at `capture.c:173` and `capture.c:246` are the library's
own outbound `connect` to its capture socket and must be left alone.

- [ ] **Step 2: Replace the macOS branch**

In `uterm_connect` (the `DYLD_INTERPOSE` branch), replace the whole
`if (addr->sa_family == AF_INET) { ... }` chain with:

```c
        char addrstr[256];
        const size_t addrstr_len =
            capture_format_sockaddr(addr, addrlen, addrstr, sizeof(addrstr));
        if (addrstr_len > 0) send_frame(CHANNEL_CONNECT, addrstr, addrstr_len);
```

Delete the now-unused `char addrstr[256] = {0};` declaration above it and the
trailing `if (addrstr[0]) send_frame(...)` line. Note the emitted length now
comes from the formatter's return value rather than a second `strlen` pass.

- [ ] **Step 3: Replace the Linux branch**

Apply the identical replacement in the `UTERM_EXPORT int connect(...)` body
(the `LD_PRELOAD` branch). The surrounding code differs — it uses
`orig_connect` rather than `g_real_connect` — but the formatting block and its
replacement are the same.

- [ ] **Step 4: Build and run the whole native suite**

Run: `cd packages/provide-uterm-platform/native/capture && make clean && make test`

Expected: the library builds with no warnings (`-Werror` is on), both test
binaries report all tests passed, and `test_symbols.sh` passes — confirming
`capture_format_sockaddr` did not become an exported symbol.

- [ ] **Step 5: Verify the defect is gone**

Run: `cd packages/provide-uterm-platform/native/capture && grep -c "inet_ntoa" capture.c`

Expected: `0`

Run: `grep -n "sun_path" capture.c`

Expected: two hits only, both in the library's own outbound connect setup
(`strncpy(addr.sun_path, path, ...)`), neither in a formatting path.

- [ ] **Step 6: Commit**

```bash
git add packages/provide-uterm-platform/native/capture/capture.c
git commit -m "refactor(native): both connect hooks share one address formatter

The macOS interpose branch and the Linux LD_PRELOAD branch carried
byte-identical formatting blocks, so the unbounded read and the
non-reentrant inet_ntoa each existed twice and had to be fixed twice.

Call the shared bounded formatter from both. The emitted length is now
the formatter's return value rather than a second strlen pass over the
same buffer."
```

---

### Task 5: Sanitizer build over the hostile inputs

**Files:**
- Modify: `packages/provide-uterm-platform/native/capture/Makefile`

**Interfaces:**
- Consumes: `test_capture_addr` target from Task 1.
- Produces: `make test-asan` target.

A bounds bug is exactly what a sanitizer catches and a passing assertion does
not: Task 1's tests would still pass if the formatter read past the struct and
happened to find a NUL. This task makes the read itself checked.

- [ ] **Step 1: Add the sanitizer target**

In `Makefile`, add after the `ADDR_TEST_TARGET` rule:

```make
ASAN_CFLAGS = $(TEST_CFLAGS) -fsanitize=address,undefined -fno-omit-frame-pointer -g

# The address tests feed deliberately unterminated and over-long inputs, which
# is precisely the shape a passing assertion can hide: reading past the struct
# still finds *a* NUL eventually. ASan makes the read itself the failure.
.PHONY: test-asan
test-asan: test_capture_addr.c capture_writer.c capture_writer.h
	cc $(ASAN_CFLAGS) -o test_capture_addr_asan test_capture_addr.c capture_writer.c
	./test_capture_addr_asan
	rm -f test_capture_addr_asan
	rm -rf test_capture_addr_asan.dSYM
```

Add the artifacts to `clean`:

```make
	rm -f test_capture_addr_asan
	rm -rf test_capture_addr_asan.dSYM
```

- [ ] **Step 2: Run it**

Run: `cd packages/provide-uterm-platform/native/capture && make test-asan`

Expected: `test_capture_addr: all tests passed` with no sanitizer report.

- [ ] **Step 3: Prove the sanitizer would have caught the original defect**

Temporarily restore the defect to confirm the harness has teeth. In
`capture_writer.c`, change the `AF_UNIX` pathname formatting to the original
unbounded form:

```c
        const int n = snprintf(out, out_len, "unix:%s", un->sun_path);
        return (n > 0 && (size_t)n < out_len) ? (size_t)n : 0;
```

Run: `cd packages/provide-uterm-platform/native/capture && make test-asan`

Expected: FAIL. `test_unterminated_path_stops_at_addrlen` either asserts or ASan
reports a stack-buffer-overflow read in `capture_format_sockaddr`. Either
outcome proves the test is load-bearing.

Then revert the edit:

Run: `git checkout packages/provide-uterm-platform/native/capture/capture_writer.c`

Run: `make test-asan` again and confirm it passes.

- [ ] **Step 4: Commit**

```bash
git add packages/provide-uterm-platform/native/capture/Makefile
git commit -m "test(native): run the address tests under ASan and UBSan

The address tests feed unterminated and over-long inputs on purpose,
which is the shape an assertion can hide: a read past the struct still
finds a NUL eventually and the string still compares equal. The
sanitizer makes the read itself the failure.

Verified by reinstating the unbounded snprintf and confirming the target
goes red."
```

---

## Definition of done

Per the measurement spec, CM-04 closes when:

- `make test` and `make test-asan` pass in
  `packages/provide-uterm-platform/native/capture/`;
- the sanitizer target was observed failing against the pre-fix formatter
  (Task 5, Step 3) — a test that cannot go red does not close a finding;
- `grep -c "inet_ntoa" capture.c` returns 0;
- no `sun_path` appears in any formatting path in `capture.c`.

Then update the CM-04 row and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- `offsetof(struct sockaddr_un, sun_path)` is 2 on both Linux and macOS, but the
  code must not assume that — macOS carries an `sun_len` byte that Linux does
  not, and the layout is exactly the kind of thing that differs.
- `sizeof(sun_path)` is 108 on Linux and 104 on macOS. Task 2's clamp test
  computes the expectation from `sizeof(un.sun_path)` rather than hardcoding
  either, so it passes on both.
- Abstract sockets are Linux-only. The test constructs the byte pattern directly
  rather than binding a real socket, so it runs and passes on macOS too — the
  formatter's behavior is what is under test, not the kernel's.
- The design doc for this work is
  `docs/superpowers/specs/2026-08-02-uterm-semantic-safety-convergence-design.md`,
  section "Native capture address formatting". It also asks for shared golden
  cases across Linux and macOS; the tests here are compiled from one source on
  both platforms, which satisfies that without a separate fixture file.
