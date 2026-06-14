#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SCAN_ROOTS: Final[tuple[str, ...]] = (
    "packages/provide-uterm/src",
    "packages/provide-uterm-server/src",
    "packages/provide-uterm-client/src",
    "packages/provide-uterm-platform/src",
    "packages/provide-uterm-frontend/src",
    "packages/provide-uterm-app/src",
    "scripts",
)
CONTROL_PATH_MARKERS: Final[tuple[str, ...]] = (
    "control",
    "terminal",
    "hijack",
    "websocket",
    "ws_session",
    "control_ws",
)
SEND_RE: Final[re.Pattern[str]] = re.compile(r"\.\s*send\s*\(\s*JSON\.stringify\s*\(")
ASSIGN_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*JSON\.stringify\s*\(")
SEND_VAR_RE: Final[re.Pattern[str]] = re.compile(r"\.\s*send\s*\(\s*([A-Za-z_$][\w$]*)\s*\)")


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str


def _is_control_ws_path(path: Path) -> bool:
    normalized = path.as_posix().lower()
    return any(marker in normalized for marker in CONTROL_PATH_MARKERS)


def _attr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _attr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _is_json_dumps_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dumps"
        and _attr_name(node.func.value) in {"json", "_json"}
    )


def _is_binary_http_tunnel_frame(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or _attr_name(node.func) != "encode_frame" or not node.args:
        return False
    first = node.args[0]
    return isinstance(first, ast.Name) and first.id == "CHANNEL_HTTP"


class _PythonBareJsonWsVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[Violation] = []
        self.json_vars: set[str] = set()
        self.json_helpers: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if any(isinstance(stmt, ast.Return) and _is_json_dumps_call(stmt.value) for stmt in node.body):
            self.json_helpers.add(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if _is_json_dumps_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.json_vars.add(target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None and _is_json_dumps_call(node.value):
            self.json_vars.add(node.target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "send" and node.args:
            target = _attr_name(node.func.value).lower()
            if any(marker in target for marker in ("ws", "websocket", "control")):
                payload = node.args[0]
                if _is_json_dumps_call(payload):
                    self.violations.append(
                        Violation(self.path, node.lineno, "bare JSON WebSocket send; use a framed control/tunnel codec")
                    )
                elif isinstance(payload, ast.Name) and payload.id in self.json_vars:
                    self.violations.append(
                        Violation(
                            self.path,
                            node.lineno,
                            f"bare JSON WebSocket send via JSON-serialized variable '{payload.id}'",
                        )
                    )
                elif (
                    isinstance(payload, ast.Call)
                    and isinstance(payload.func, ast.Name)
                    and payload.func.id in self.json_helpers
                    and not _is_binary_http_tunnel_frame(payload)
                ):
                    self.violations.append(
                        Violation(
                            self.path,
                            node.lineno,
                            f"bare JSON WebSocket send via JSON helper '{payload.func.id}'",
                        )
                    )
        self.generic_visit(node)


def _python_violations(path: Path) -> list[Violation]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _PythonBareJsonWsVisitor(path)
    visitor.visit(tree)
    return visitor.violations


def _typescript_violations(path: Path) -> list[Violation]:
    violations: list[Violation] = []
    json_vars: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if "fetch(" in line or "body:" in line:
            continue
        for match in ASSIGN_RE.finditer(line):
            json_vars.add(match.group(1))
        if SEND_RE.search(line):
            violations.append(Violation(path, line_no, "bare JSON WebSocket send; use a framed control/tunnel codec"))
            continue
        var_match = SEND_VAR_RE.search(line)
        if var_match and var_match.group(1) in json_vars:
            violations.append(
                Violation(
                    path, line_no, f"bare JSON WebSocket send via JSON-serialized variable '{var_match.group(1)}'"
                )
            )
    return violations


def find_violations(paths: list[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for path in paths:
        if not path.is_file() or not _is_control_ws_path(path):
            continue
        if path.suffix == ".py":
            violations.extend(_python_violations(path))
        elif path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
            violations.extend(_typescript_violations(path))
    return violations


def _default_paths() -> list[Path]:
    paths: list[Path] = []
    for root in SCAN_ROOTS:
        base = Path(root)
        if not base.exists():
            continue
        for suffix in ("*.py", "*.ts", "*.tsx", "*.js", "*.jsx"):
            paths.extend(base.rglob(suffix))
    return paths


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    paths = [Path(arg) for arg in args] if args else _default_paths()
    violations = find_violations(paths)
    for violation in violations:
        print(f"{violation.path}:{violation.line}: {violation.message}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
