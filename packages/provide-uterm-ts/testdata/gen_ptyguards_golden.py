#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the PTY connector's guards.

Everything here runs *before* a fork, an execve, a PAM call or a bind — which
is the whole reason it exists. Once any of those has happened the value is
already in the kernel's hands.

* **Null bytes.** Every string that reaches a system call is checked for one,
  because C stops reading at the first and a value that looks like
  ``/bin/sh\\0; rm -rf /`` is two different strings depending on who reads it.
* **Absolute paths only.** A command is refused unless it starts with ``/``:
  a relative path or a bare name would be resolved against whatever ``PATH``
  happened to be, which is the caller's environment and not the operator's
  intent.
* **An environment key with an ``=`` in it.** Refused, because ``execve``
  joins keys and values with one and a key carrying its own would inject a
  second variable.
* **Privilege.** ``UidMap`` refuses to resolve anything to uid 0 or gid 0
  unless an operator has explicitly allowed root — the difference between a
  session that runs as a user and one that runs as the machine.

The passwd database is the one thing here that is not pure, so the corpus
records the resolution against a fixed table of users rather than the running
machine's.

# uv-package: provide-uterm-platform

Usage (from the repository root)::

    uv run --package provide-uterm-platform python \\
        packages/provide-uterm-ts/testdata/gen_ptyguards_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from provide.uterm.pty import _validate, uid_map
from provide.uterm.pty.socket_utils import validate_socket_path

OUT = Path(__file__).resolve().parent / "ptyguards_golden.json"

# A passwd database that does not depend on the machine this runs on.
PASSWD: dict[str, tuple[int, int, str, str]] = {
    "root": (0, 0, "/root", "/bin/bash"),
    "ada": (1000, 1000, "/home/ada", "/bin/zsh"),
    "grace": (1001, 2000, "/home/grace", "/bin/sh"),
    "daemon": (2, 2, "/usr/sbin", "/usr/sbin/nologin"),
}


class FakePwEntry:
    """What `pwd` hands back, with only the fields the map reads."""

    def __init__(self, name: str, uid: int, gid: int, home: str, shell: str) -> None:
        self.pw_name = name
        self.pw_uid = uid
        self.pw_gid = gid
        self.pw_dir = home
        self.pw_shell = shell


def _getpwnam(name: str) -> FakePwEntry:
    if name not in PASSWD:
        raise KeyError(name)
    uid, gid, home, shell = PASSWD[name]
    return FakePwEntry(name, uid, gid, home, shell)


def _getpwuid(uid: int) -> FakePwEntry:
    for name, (entry_uid, gid, home, shell) in PASSWD.items():
        if entry_uid == uid:
            return FakePwEntry(name, uid, gid, home, shell)
    raise KeyError(uid)


def _outcome(call: Any) -> dict[str, Any]:
    """Whether the reference accepts this, and what it says if not."""
    try:
        call()
    except ValueError as exc:
        return {"error": str(exc)}
    return {"error": None}


COMMANDS: list[tuple[str, str]] = [
    ("an absolute path", "/bin/bash"),
    ("nothing at all", ""),
    ("a bare name", "bash"),
    ("a relative path", "./bash"),
    ("a path up and out", "../bin/bash"),
    ("a path with a null byte in it", "/bin/sh\x00; rm -rf /"),
    ("a path that ends in a null byte", "/bin/sh\x00"),
    ("a path with a space in it", "/usr/local/my shell"),
    ("a path 4096 characters long", "/" + "a" * 4095),
    ("a path 4097 characters long", "/" + "a" * 4096),
    ("a path that is only a slash", "/"),
]

USERNAMES: list[tuple[str, str]] = [
    ("an ordinary name", "ada"),
    ("nothing at all", ""),
    ("a name with a null byte in it", "ada\x00root"),
    ("a name with dots, dashes and underscores", "ada.b_c-d"),
    ("a name with a slash in it", "../root"),
    ("a name with a space in it", "ada b"),
    ("a name with a colon in it", "ada:root"),
    ("a name with a newline in it", "ada\nroot"),
    ("a name written in French", "adaé"),
    ("a name of digits", "1000"),
    ("a name 255 characters long", "a" * 255),
    ("a name 256 characters long", "a" * 256),
]

SERVICES: list[tuple[str, str]] = [
    ("an ordinary service", "login"),
    ("nothing at all", ""),
    ("a service with a null byte in it", "login\x00"),
    ("a service with dashes and underscores", "uterm-pty_1"),
    ("a service with a dot in it", "login.d"),
    ("a service with a slash in it", "../login"),
    ("a service 255 characters long", "a" * 255),
    ("a service 256 characters long", "a" * 256),
]

ENVS: list[tuple[str, dict[str, str]]] = [
    ("an ordinary environment", {"HOME": "/home/ada", "TERM": "xterm"}),
    ("nothing at all", {}),
    ("a key with an equals in it", {"A=B": "c"}),
    ("a key with a null byte in it", {"A\x00B": "c"}),
    ("a value with a null byte in it", {"A": "b\x00c"}),
    ("a value 65536 characters long", {"A": "b" * 65536}),
    ("a value 65537 characters long", {"A": "b" * 65537}),
    ("a thousand keys", {f"K{index}": "v" for index in range(1000)}),
    ("a thousand and one keys", {f"K{index}": "v" for index in range(1001)}),
    ("an empty key", {"": "v"}),
]

SOCKET_PATHS: list[tuple[str, str]] = [
    ("an absolute path", "/run/uterm/pty.sock"),
    ("a relative path", "run/pty.sock"),
    ("a path with a null byte in it", "/run/pty.sock\x00"),
    ("nothing at all", ""),
    ("an abstract socket, which Linux writes with a leading null", "\x00uterm"),
]

# username, table, allow_root, run_as, run_as_uid, run_as_gid
RESOLVE_CASES: list[tuple[str, dict[str, Any]]] = [
    ("a user running as themselves", {"username": "ada"}),
    ("a user nobody has heard of", {"username": "nobody-here"}),
    ("a user with a bad name", {"username": "ada:root"}),
    ("root, running as themselves", {"username": "root"}),
    ("root, allowed", {"username": "root", "allow_root": True}),
    ("a user whose group is root", {"username": "grace", "run_as_gid": 0}),
    ("an explicit uid", {"username": "ada", "run_as_uid": 1001}),
    ("an explicit uid of root", {"username": "ada", "run_as_uid": 0}),
    ("an explicit uid of root, allowed", {"username": "ada", "run_as_uid": 0, "allow_root": True}),
    ("an explicit gid of root", {"username": "ada", "run_as_uid": 1001, "run_as_gid": 0}),
    ("an explicit uid nobody has", {"username": "ada", "run_as_uid": 4242}),
    ("an explicit uid nobody has, with a group", {"username": "ada", "run_as_uid": 4242, "run_as_gid": 77}),
    ("a run-as name", {"username": "ada", "run_as": "grace"}),
    ("a run-as name nobody has", {"username": "ada", "run_as": "nobody-here"}),
    ("a run-as uid written as a string", {"username": "ada", "run_as": "1001"}),
    ("a run-as uid and gid", {"username": "ada", "run_as": "1001:2001"}),
    ("a run-as uid and gid, both root", {"username": "ada", "run_as": "0:0"}),
    ("a run-as name with a group of its own", {"username": "ada", "run_as": "grace", "run_as_gid": 3000}),
    ("a table entry", {"username": "ada", "table": {"ada": "grace"}}),
    ("a table wildcard", {"username": "ada", "table": {"*": "grace"}}),
    ("a table entry beating the wildcard", {"username": "ada", "table": {"ada": "grace", "*": "daemon"}}),
    ("a table entry for somebody else", {"username": "ada", "table": {"grace": "daemon"}}),
    ("a table entry naming a uid", {"username": "ada", "table": {"ada": "1001:2001"}}),
    ("a run-as beating the table", {"username": "ada", "table": {"ada": "daemon"}, "run_as": "grace"}),
    ("an explicit uid beating the run-as", {"username": "ada", "run_as": "grace", "run_as_uid": 1001}),
    ("an explicit uid beating a different run-as", {"username": "ada", "run_as": "daemon", "run_as_uid": 1001}),
    ("a run-as spec with two colons in it", {"username": "ada", "run_as": "1000:2000:3000"}),
    ("a run-as spec whose group is not a number", {"username": "ada", "run_as": "1000:root"}),
    ("a table entry naming root", {"username": "ada", "table": {"ada": "root"}}),
    ("no username at all", {"username": "", "run_as_uid": 1000}),
]


def _resolve(case: dict[str, Any]) -> dict[str, Any]:
    mapper = uid_map.UidMap(case.get("table"), allow_root=case.get("allow_root", False))
    kwargs = {key: case[key] for key in ("run_as", "run_as_uid", "run_as_gid") if key in case}
    try:
        with patch.object(uid_map.pwd, "getpwnam", _getpwnam), patch.object(uid_map.pwd, "getpwuid", _getpwuid):
            resolved = mapper.resolve(case["username"], **kwargs)
    except ValueError as exc:
        return {"error": str(exc), "type": type(exc).__name__}
    return {
        "error": None,
        "resolved": {
            "uid": resolved.uid,
            "gid": resolved.gid,
            "home": resolved.home,
            "shell": resolved.shell,
            "name": resolved.name,
        },
    }


REPR_CASES: list[str] = [
    "ada",
    "",
    "ada\nroot",
    "ada\troot",
    "ada\rroot",
    "ada\x00root",
    "ada\x1broot",
    "ada\x7froot",
    "it's",
    'say "hi"',
    "both ' and \"",
    "back\\slash",
    "adaé",
    "端末",
    "\x01\x02",
]


def main() -> None:
    corpus = {
        # The refusals quote the value back with Python's ``repr``, which
        # escapes a control character rather than printing it — the difference
        # between a message an operator can read and one that moves their
        # cursor.
        "reprs": [{"value": value, "repr": repr(value)} for value in REPR_CASES],
        "passwd": {
            name: {"uid": uid, "gid": gid, "home": home, "shell": shell}
            for name, (uid, gid, home, shell) in PASSWD.items()
        },
        "commands": [
            {"name": name, "value": value, **_outcome(lambda v=value: _validate.validate_command(v))}
            for name, value in COMMANDS
        ],
        "usernames": [
            {"name": name, "value": value, **_outcome(lambda v=value: _validate.validate_username(v))}
            for name, value in USERNAMES
        ],
        "services": [
            {"name": name, "value": value, **_outcome(lambda v=value: _validate.validate_service_name(v))}
            for name, value in SERVICES
        ],
        "envs": [
            {
                "name": name,
                # A thousand keys is a thousand lines of corpus, so only the
                # shape is recorded for the big ones.
                "value": value if len(value) <= 4 else {"__generated__": len(value)},
                **_outcome(lambda v=value: _validate.validate_env(v)),
            }
            for name, value in ENVS
        ],
        "socket_paths": [
            {"name": name, "value": value, **_outcome(lambda v=value: validate_socket_path(v))}
            for name, value in SOCKET_PATHS
        ],
        "resolve": [{"name": name, "case": case, **_resolve(case)} for name, case in RESOLVE_CASES],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['resolve'])} resolutions)")


if __name__ == "__main__":
    main()
