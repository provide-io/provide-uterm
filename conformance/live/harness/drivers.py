#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Starting drivers and reading what they say.

Nothing here is in-process. A server driver is a real process listening on a
real port, and a client driver is a real process talking to it over a real
socket — that is the whole difference between this harness and the offline
corpora, which prove decisions and prove nothing about wiring.

Two rules the mechanics enforce, because a harness that hangs is worse than
one that fails:

* a driver that never announces itself is an error at a deadline, not a wait;
* a driver that ignores the polite ask to stop is killed.
"""

from __future__ import annotations

import contextlib
import json
import queue
import subprocess  # nosec B404 - running drivers is the point
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

import jsonschema
from harness.scenario import schema

#: How long to wait after closing stdin before insisting.
DEFAULT_GRACE_S: Final = 5.0
#: How much of a driver's own output to quote back in a refusal.
_QUOTE_LIMIT: Final = 2000


class DriverError(RuntimeError):
    """A driver did not hold up its half of the protocol."""


@dataclass(frozen=True)
class DriverSpec:
    """How to start one language's driver."""

    language: str
    command: tuple[str, ...]
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ServerHandle:
    """A running server driver and what it said about itself."""

    language: str
    base_url: str
    token: str
    capabilities: tuple[str, ...]
    process: subprocess.Popen[str]
    _stderr: _Collector

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def stop(self, grace_s: float = DEFAULT_GRACE_S) -> None:
        """Ask the driver to stop, then insist, then stop asking."""
        with contextlib.suppress(OSError, ValueError):
            if self.process.stdin is not None:
                self.process.stdin.close()
        for signal_it in (self.process.terminate, self.process.kill):
            try:
                self.process.wait(timeout=grace_s)
                return
            except subprocess.TimeoutExpired:
                signal_it()
        self.process.wait(timeout=grace_s)


@contextlib.contextmanager
def start_server(
    spec: DriverSpec,
    *,
    auth: str,
    timeout_s: float,
    grace_s: float = DEFAULT_GRACE_S,
) -> Iterator[ServerHandle]:
    """Start *spec* in its server role and yield it once it has announced."""
    process = _spawn(spec, ("serve", "--auth", auth))
    stdout, stderr = _Reader(process.stdout), _Collector(process.stderr)
    try:
        announcement = _announcement(process, stdout, stderr, spec, timeout_s)
    except DriverError:
        _kill(process)
        raise
    handle = ServerHandle(
        language=spec.language,
        base_url=str(announcement["base_url"]),
        token=str(announcement.get("token", "")),
        capabilities=tuple(announcement.get("capabilities", ())),
        process=process,
        _stderr=stderr,
    )
    try:
        yield handle
    finally:
        handle.stop(grace_s)


def run_client(
    spec: DriverSpec,
    *,
    scenario_path: Path,
    base_url: str,
    token: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Run *spec* in its client role and return the result it wrote."""
    argv = (
        "client",
        "--base-url",
        base_url,
        "--token",
        token,
        "--scenario",
        str(scenario_path),
    )
    try:
        finished = subprocess.run(  # nosec B603 - the command is a registered driver
            [*spec.command, *argv],
            cwd=str(spec.cwd) if spec.cwd else None,
            env=_env(spec),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        raise DriverError(f"{spec.language} client driver timed out after {timeout_s}s") from expired
    return _result(spec, finished)


def _result(spec: DriverSpec, finished: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The last line the client wrote, held to the result schema."""
    lines = [line for line in finished.stdout.splitlines() if line.strip()]
    if not lines:
        raise DriverError(
            f"{spec.language} client driver wrote nothing to stdout "
            f"(exit {finished.returncode}){_quote('stderr', finished.stderr)}"
        )
    line = lines[-1]
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as error:
        raise DriverError(
            f"{spec.language} client driver's last line is not JSON: {error}{_quote('line', line)}"
        ) from error
    try:
        jsonschema.validate(parsed, schema("result"))
    except jsonschema.ValidationError as error:
        raise DriverError(
            f"{spec.language} client driver's result does not match the result schema: "
            f"{error.message}{_quote('line', line)}"
        ) from error
    return dict(parsed)


def _announcement(
    process: subprocess.Popen[str],
    stdout: _Reader,
    stderr: _Collector,
    spec: DriverSpec,
    timeout_s: float,
) -> dict[str, Any]:
    """Read lines until one is a server announcement, or give up saying so."""
    deadline = time.monotonic() + timeout_s
    while True:
        line = stdout.next_line(deadline - time.monotonic())
        if line is None:
            break
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(line)
            if isinstance(parsed, dict) and parsed.get("role") == "server" and "base_url" in parsed:
                return parsed
    raise DriverError(
        f"{spec.language} server driver did not announce a base_url "
        f"(exit {process.poll()}){_quote('stderr', stderr.text())}"
    )


def _spawn(spec: DriverSpec, argv: Sequence[str]) -> subprocess.Popen[str]:
    """Start a driver with pipes on all three streams."""
    return subprocess.Popen(  # nosec B603 - the command is a registered driver
        [*spec.command, *argv],
        cwd=str(spec.cwd) if spec.cwd else None,
        env=_env(spec),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _env(spec: DriverSpec) -> dict[str, str] | None:
    """The driver's environment, or the harness's own when it adds nothing."""
    if not spec.env:
        return None
    import os

    return {**os.environ, **spec.env}


def _kill(process: subprocess.Popen[str]) -> None:
    """Stop a driver that never got as far as being usable."""
    with contextlib.suppress(OSError, ValueError):
        if process.stdin is not None:
            process.stdin.close()
    with contextlib.suppress(OSError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=DEFAULT_GRACE_S)


def _quote(label: str, text: str) -> str:
    """Quote a driver's own output back, so a refusal says what happened."""
    text = text.strip()
    if not text:
        return ""
    clipped = text[:_QUOTE_LIMIT]
    return f"\n  {label}: {clipped}"


class _Reader:
    """Lines from a pipe, readable with a deadline rather than forever."""

    def __init__(self, stream: Any) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: Any) -> None:
        with contextlib.suppress(ValueError, OSError):
            for line in stream:
                self._lines.put(line.rstrip("\n"))
        self._lines.put(None)

    def next_line(self, timeout_s: float) -> str | None:
        """The next line, or ``None`` at end of stream or out of time."""
        if timeout_s <= 0:
            return None
        try:
            return self._lines.get(timeout=timeout_s)
        except queue.Empty:
            return None


class _Collector:
    """Everything a pipe ever produced, gathered off the main thread.

    A driver whose stderr filled the pipe buffer would block forever, and the
    text is what a refusal quotes back, so it is always drained.
    """

    def __init__(self, stream: Any) -> None:
        self._chunks: list[str] = []
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: Any) -> None:
        with contextlib.suppress(ValueError, OSError):
            for line in stream:
                self._chunks.append(line)

    def text(self) -> str:
        self._thread.join(timeout=1.0)
        return "".join(self._chunks)
