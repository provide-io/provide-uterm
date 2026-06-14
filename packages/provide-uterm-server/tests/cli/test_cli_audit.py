#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the ``uterm audit verify`` CLI subcommand."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from provide.uterm.cli import _build_parser, main
from provide.uterm.server.audit_chain import AuditChain

pytestmark = pytest.mark.timeout(5)


def _good_log(tmp_path: Path) -> tuple[Path, AuditChain]:
    """Write a small valid audit log and return its path + chain."""
    path = tmp_path / "audit.log"
    chain = AuditChain(path, clock=lambda: 1.0, mono=lambda: 1)
    chain.append("a", principal="alice")
    chain.append("b", principal="bob")
    return path, chain


def _run(argv: list[str]) -> tuple[int, str]:
    """Invoke main() capturing exit code + stdout."""
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out):
        try:
            main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue()


class TestAuditCli:
    def test_parser_registers_audit_verify(self) -> None:
        args = _build_parser().parse_args(["audit", "verify", "/tmp/x.log"])
        assert args.command == "audit"
        assert args.path == "/tmp/x.log"
        assert callable(args.func)

    def test_verify_good_exits_zero(self, tmp_path: Path) -> None:
        path, _ = _good_log(tmp_path)
        code, out = _run(["audit", "verify", str(path)])
        assert code == 0
        assert out.startswith("OK:")
        assert "head seq=2" in out

    def test_verify_tampered_exits_one(self, tmp_path: Path) -> None:
        path, _ = _good_log(tmp_path)
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        records[0]["action"] = "TAMPERED"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        code, out = _run(["audit", "verify", str(path)])
        assert code == 1
        assert out.startswith("TAMPERED:")
        assert "seq=1" in out

    def test_verify_missing_file_exits_one(self, tmp_path: Path) -> None:
        code, out = _run(["audit", "verify", str(tmp_path / "nope.log")])
        assert code == 1
        assert "TAMPERED:" in out
        assert "not found" in out

    def test_expected_head_match_exits_zero(self, tmp_path: Path) -> None:
        path, chain = _good_log(tmp_path)
        code, out = _run(["audit", "verify", str(path), "--expected-seq", "2", "--expected-hash", chain.last_hash])
        assert code == 0
        assert out.startswith("OK:")

    def test_expected_head_mismatch_exits_one(self, tmp_path: Path) -> None:
        path, _ = _good_log(tmp_path)
        code, out = _run(["audit", "verify", str(path), "--expected-seq", "2", "--expected-hash", "f" * 64])
        assert code == 1
        assert "head" in out

    def test_only_expected_seq_errors_exit_two(self, tmp_path: Path) -> None:
        path, _ = _good_log(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["audit", "verify", str(path), "--expected-seq", "2"])
        assert exc.value.code == 2

    def test_only_expected_hash_errors_exit_two(self, tmp_path: Path) -> None:
        path, _ = _good_log(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["audit", "verify", str(path), "--expected-hash", "f" * 64])
        assert exc.value.code == 2


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-vv"])
