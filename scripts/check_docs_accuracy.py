#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import re
from pathlib import Path

DOC_PATHS = ("README.md", "docs")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
MUTATION_CMD_RE = re.compile(r"run_mutation_gate\.py\b")
MIN_MUTATION_RE = re.compile(r"--min-mutation-score\s+100(?:\.0)?\b")


def _iter_markdown_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for entry in DOC_PATHS:
        path = root / entry
        if path.is_file():
            files.append(path)
            continue
        if path.is_dir():
            files.extend(sorted(path.rglob("*.md")))
    return sorted(set(files))


def _slugify_heading(text: str) -> str:
    lowered = text.strip().lower()
    cleaned = re.sub(r"[^\w\s-]", "", lowered)
    collapsed = re.sub(r"\s+", "-", cleaned)
    collapsed = re.sub(r"-{2,}", "-", collapsed)
    return collapsed.strip("-")


def _extract_anchors(content: str) -> set[str]:
    anchors: set[str] = set()
    in_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match is None:
            continue
        anchors.add(_slugify_heading(match.group(2)))
    return anchors


def _is_external_link(link: str) -> bool:
    return link.startswith(("http://", "https://", "mailto:"))


def _style_violations(path: Path, content: str) -> list[str]:
    violations: list[str] = []
    lines = content.splitlines()
    if not lines:
        violations.append(f"{path}: empty markdown file")
        return violations

    first_non_empty = next((line for line in lines if line.strip()), "")
    if not first_non_empty.startswith("# "):
        violations.append(f"{path}: first non-empty line must be H1 heading")

    prev_level = 0
    in_fence = False
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        if raw.rstrip() != raw:
            violations.append(f"{path}:{number}: trailing whitespace")
        if "\t" in raw and not in_fence:
            violations.append(f"{path}:{number}: tab character outside code fence")
        if in_fence:
            continue
        heading = HEADING_RE.match(raw)
        if heading is None:
            continue
        level = len(heading.group(1))
        if prev_level and level > prev_level + 1:
            violations.append(f"{path}:{number}: heading level jumps from H{prev_level} to H{level}")
        prev_level = level
    return violations


def _link_violations(path: Path, content: str, anchor_map: dict[Path, set[str]]) -> list[str]:
    violations: list[str] = []
    for match in LINK_RE.finditer(content):
        link = match.group(1)
        if _is_external_link(link):
            continue
        if link.startswith("app://"):
            continue

        target_path: Path
        anchor: str | None = None
        if "#" in link:
            raw_path, anchor = link.split("#", 1)
        else:
            raw_path = link
        target_path = path if raw_path == "" else (path.parent / raw_path).resolve()
        if not target_path.exists():
            violations.append(f"{path}: missing link target {link}")
            continue
        if anchor:
            anchors = anchor_map.get(target_path, set())
            if anchor not in anchors:
                violations.append(f"{path}: missing anchor #{anchor} in {target_path}")
    return violations


def _claim_violations(path: Path, content: str) -> list[str]:
    violations: list[str] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        if "uv run" in line and MUTATION_CMD_RE.search(line) and not MIN_MUTATION_RE.search(line):
            violations.append(f"{path}:{line_no}: run_mutation_gate command must include --min-mutation-score 100")
    return violations


def _architecture_diagram_violations(path: Path, content: str) -> list[str]:
    violations: list[str] = []
    if path.name != "ARCHITECTURE.md":
        return violations
    mermaid_blocks = content.count("```mermaid")
    if mermaid_blocks < 2:
        violations.append(f"{path}: expected at least two mermaid diagrams")
    if "flowchart" not in content:
        violations.append(f"{path}: missing flowchart diagram")
    if "sequenceDiagram" not in content:
        violations.append(f"{path}: missing sequence diagram")
    return violations


def check_docs(root: Path) -> list[str]:
    markdown_files = _iter_markdown_files(root)
    anchor_map: dict[Path, set[str]] = {}
    contents: dict[Path, str] = {}
    for file_path in markdown_files:
        content = file_path.read_text(encoding="utf-8")
        resolved = file_path.resolve()
        contents[resolved] = content
        anchor_map[resolved] = _extract_anchors(content)

    violations: list[str] = []
    for resolved_path, content in contents.items():
        violations.extend(_style_violations(resolved_path, content))
        violations.extend(_link_violations(resolved_path, content, anchor_map))
        violations.extend(_claim_violations(resolved_path, content))
        violations.extend(_architecture_diagram_violations(resolved_path, content))
    return sorted(violations)


def main() -> int:
    root = Path.cwd()
    violations = check_docs(root)
    if violations:
        for item in violations:
            print(item)
        return 1
    print("docs accuracy checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
