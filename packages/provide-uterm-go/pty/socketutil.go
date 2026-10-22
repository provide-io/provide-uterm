//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"errors"
	"fmt"
	"net"
	"os"
	"strings"
	"sync"
	"syscall"
	"time"
)

// chmodSocket tightens a freshly bound socket's mode (os.Chmod). A variable
// only so a test can fail it: on a socket this process just created, the chmod
// cannot be made to fail from outside.
var chmodSocket = os.Chmod

// ValidateSocketPath rejects Unix socket paths that aren't absolute or contain
// null bytes. Port of socket_utils.validate_socket_path.
func ValidateSocketPath(path string) error {
	if strings.ContainsRune(path, '\x00') {
		return fmt.Errorf("socket path contains null byte")
	}
	if !strings.HasPrefix(path, "/") {
		return fmt.Errorf("socket path must be an absolute path")
	}
	return nil
}

// probeTimeout is how long a probe of a socket already at the path may take
// to be answered.
var probeTimeout = 100 * time.Millisecond

// removeStaleSocket removes a socket left at path by a process that died
// without unbinding it (SIGKILL, OOM), which would otherwise fail the next bind
// with EADDRINUSE. Only a stale one goes: a socket whose connect is refused.
// One something still listens on, another session's, stays, and the bind
// fails on it rather than taking its producer away; so does one that does not
// say (a connect refused for another reason, or not answered in time). Lstat
// does not follow a symlink at path, and a regular file, directory or link is
// somebody else's and is left for the bind to fail on. Port of the
// reference's _clear_stale_socket.
func removeStaleSocket(path string) {
	info, err := os.Lstat(path)
	if err != nil || info.Mode()&os.ModeSocket == 0 {
		return
	}
	conn, err := net.DialTimeout("unix", path, probeTimeout)
	if err == nil {
		_ = conn.Close()
		return
	}
	if errors.Is(err, syscall.ECONNREFUSED) {
		_ = os.Remove(path)
	}
}

// bindUmaskMu serialises every umask set, bind and restore in this package.
// The umask is process-wide: two binds running at once interleave, the second
// reading the first's bind umask as the one to restore, and leave it behind
// for every file the process creates afterwards. At a capture mode of 0o660
// that is 0o117, wider than a service's 0o077 in the group bits. Port of the
// reference's synchronous bind, which its single-threaded event loop makes
// atomic. A file another goroutine creates inside the window still sees the
// bind's umask, which is why the window is the bind call alone.
var bindUmaskMu sync.Mutex

// listenUnderUmask listens on the Unix socket at path, bound under mask and
// with the previous umask restored before it returns.
func listenUnderUmask(path string, mask int) (net.Listener, error) {
	bindUmaskMu.Lock()
	defer bindUmaskMu.Unlock()
	prev := umaskSet(mask)
	ln, err := net.Listen("unix", path)
	umaskSet(prev)
	return ln, err
}
