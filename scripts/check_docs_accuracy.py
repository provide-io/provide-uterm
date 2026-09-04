#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import re
from pathlib import Path

from repo_paths import submodule_dirs

DOC_PATHS = ("README.md", "docs")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
MUTATION_CMD_RE = re.compile(r"run_mutation_gate\.py\b")
MIN_MUTATION_RE = re.compile(r"--min-mutation-score\s+100(?:\.0)?\b")

#: File kinds that can carry a documented MCP tool count. Prose says it, and so
#: do package docstrings and the Go command's header comment -- which is what
#: the generated Go API reference publishes.
TOOL_COUNT_SUFFIXES = frozenset({".md", ".py", ".go", ".ts", ".mjs"})

#: Directory names never walked. Vendored trees, build output and caches make
#: claims that are not this repo's to make, and `archive` holds documents whose
#: numbers are a record of what was true when they were written -- updating
#: those to today's count would destroy the thing they exist to preserve.
TOOL_COUNT_PRUNE = frozenset(
    {
        ".git",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".wrangler",
        "__pycache__",
        "archive",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "mutants",
        "node_modules",
        "obj",
        "python_modules",
        "reports",
        "site-packages",
        "StrykerOutput",
    }
)

#: "28 tools", "28 session control tools", "the same 28 tools". Up to two words
#: may sit between the number and the noun so a qualified count still matches;
#: beyond that the number is usually about something else on the line.
TOOL_COUNT_RE = re.compile(r"\b(\d+)\s+(?:[a-z][\w-]*\s+){0,2}tools?\b", re.IGNORECASE)

#: Only lines actually talking about MCP are this check's business.
MCP_CONTEXT_RE = re.compile(r"mcp|model context protocol", re.IGNORECASE)


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

    # Skip leading HTML comment blocks (e.g. SPDX licence headers) when
    # determining the first meaningful line.
    in_html_comment = False
    first_non_empty = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<!--"):
            in_html_comment = True
        if in_html_comment:
            if "-->" in stripped:
                in_html_comment = False
            continue
        first_non_empty = line
        break
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
    # Build a set of character offsets that fall inside code fences so we can
    # skip links that appear in code blocks (e.g. f-strings, markdown examples).
    fenced_ranges: list[tuple[int, int]] = []
    in_fence = False
    fence_start = 0
    for line in content.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            if in_fence:
                fenced_ranges.append((fence_start, content.index(line, fence_start) + len(line)))
                in_fence = False
            else:
                fence_start = content.index(line)
                in_fence = True

    def _in_fence(pos: int) -> bool:
        return any(start <= pos < end for start, end in fenced_ranges)

    for match in LINK_RE.finditer(content):
        if _in_fence(match.start()):
            continue
        link = match.group(1)
        if _is_external_link(link):
            continue
        if link.startswith("app://"):
            continue
        if link.startswith("{"):  # template placeholder, not a real path
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


def _mcp_tool_counts(root: Path) -> tuple[int, dict[str, int]]:
    """``(total, {module_stem: count})`` of ``@mcp.tool`` registrations."""
    ai_dir = root / "packages/provide-uterm-client/src/provide/uterm/ai"
    if not ai_dir.is_dir():
        return 0, {}
    total = sum(path.read_text(encoding="utf-8").count("@mcp.tool") for path in sorted(ai_dir.glob("*.py")))
    per_module = {
        path.stem: path.read_text(encoding="utf-8").count("@mcp.tool")
        for path in sorted(ai_dir.glob("server_tools_*.py"))
    }
    return total, per_module


def _files_that_may_claim(root: Path) -> list[Path]:
    """Every file under *root* that could state a tool count, pruned trees aside."""
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir()):
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in TOOL_COUNT_PRUNE:
                    stack.append(entry)
            elif entry.suffix in TOOL_COUNT_SUFFIXES:
                found.append(entry)
    return found


def _tool_count_violations(root: Path) -> list[str]:
    """Every place that states an MCP tool count must state the real one.

    Until 2026-09-03 this was checked in README.md alone. README.md was right --
    and ``demo/mcp/README.md``, the ``ai/server_impl.py`` docstring, the Go
    README twice, and ``cmd/uterm-mcp/main.go`` all still said 21, seven tools
    behind. The one file under check stayed correct while every other file
    making the same claim rotted, which is the shape of drift a single-file
    check produces rather than prevents.

    A line may quote the total or any one module's share of it: the docstring
    and the roadmaps break the count down deliberately, and a breakdown that no
    longer sums is itself drift worth catching.
    """
    total, per_module = _mcp_tool_counts(root)
    if total == 0:
        return []
    allowed = {total, *per_module.values()}
    breakdown = ", ".join(f"{stem.removeprefix('server_tools_')}={count}" for stem, count in sorted(per_module.items()))
    this_file = Path(__file__).resolve()

    violations: list[str] = []
    for path in _files_that_may_claim(root):
        if path.resolve() == this_file:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "mcp" not in content.lower():
            continue
        for line_no, line in enumerate(content.splitlines(), start=1):
            if not MCP_CONTEXT_RE.search(line):
                continue
            for match in TOOL_COUNT_RE.finditer(line):
                claimed = int(match.group(1))
                if claimed in allowed:
                    continue
                violations.append(
                    f"{path}:{line_no}: says {match.group(0)!r} but there are"
                    f" {total} @mcp.tool registrations ({breakdown})"
                )
    return violations


def _structural_claim_violations(root: Path) -> list[str]:
    """Cross-file structural checks: live code/config vs documented claims."""
    violations: list[str] = []

    # --- 1. Coverage threshold ---
    # Every package pyproject.toml that enables --cov in addopts must also set
    # --cov-fail-under=100.  This reflects the "100% coverage enforced" claim.
    for pyproject in sorted((root / "packages").glob("*/pyproject.toml")):
        text = pyproject.read_text(encoding="utf-8")
        in_pytest_section = False
        addopts_lines: list[str] = []
        collecting = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "[tool.pytest.ini_options]":
                in_pytest_section = True
                continue
            if in_pytest_section and stripped.startswith("[") and stripped != "[tool.pytest.ini_options]":
                in_pytest_section = False
                collecting = False
            if in_pytest_section:
                if stripped.startswith("addopts"):
                    collecting = True
                if collecting:
                    addopts_lines.append(stripped)
                    # Stop collecting at the closing bracket of the array
                    if "]" in stripped and not stripped.startswith("addopts"):
                        collecting = False
        addopts_blob = " ".join(addopts_lines)
        if "--cov" in addopts_blob and "--cov-fail-under=100" not in addopts_blob:
            violations.append(f"{pyproject}: addopts enables --cov but is missing --cov-fail-under=100")

    # --- 2. MCP tool count ---
    # Checked repo-wide by _tool_count_violations, not here: the claim is made
    # in six places and a check that reads one of them proved to be the reason
    # the other five drifted.
    readme = root / "README.md"

    # --- 3. Package count ---
    # Count packages/ subdirectories and compare to CLAUDE.md's "N packages under packages/" claim.
    #
    # Submodules under packages/ are NOT this repo's packages. provide-telemetry
    # is vendored there as a git submodule -- a dependency that happens to live
    # in the same directory -- and counting it made the doc "wrong" the moment
    # the submodule was added, about a package the sentence was never claiming
    # and the table beneath it does not list. Read .gitmodules rather than
    # hardcoding the name, so the next vendored submodule needs no edit here.
    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        submodules = submodule_dirs(root)
        actual_pkg_count = sum(1 for p in (root / "packages").iterdir() if p.is_dir() and p.resolve() not in submodules)
        claude_text = claude_md.read_text(encoding="utf-8")
        pkg_count_match = re.search(r"(\d+)\s+packages?\s+under\s+[`']?packages/[`']?", claude_text)
        if pkg_count_match:
            doc_pkg_count = int(pkg_count_match.group(1))
            if doc_pkg_count != actual_pkg_count:
                for line_no, line in enumerate(claude_text.splitlines(), start=1):
                    if pkg_count_match.group(0) in line:
                        violations.append(
                            f"{claude_md}:{line_no}: says {doc_pkg_count} packages under packages/,"
                            f" found {actual_pkg_count} directories"
                        )
                        break

    # --- 4. Supported version ---
    # Read VERSION (e.g. "0.4.0"), derive the minor line (e.g. "0.4"), and assert
    # SECURITY.md's supported-versions table contains a row for that line (e.g. "0.4.x").
    version_file = root / "VERSION"
    security_md = root / "SECURITY.md"
    if version_file.exists() and security_md.exists():
        version_str = version_file.read_text(encoding="utf-8").strip()
        parts = version_str.split(".")
        if len(parts) >= 2:
            minor_line = f"{parts[0]}.{parts[1]}"  # e.g. "0.4"
            security_text = security_md.read_text(encoding="utf-8")
            # Look for the minor line followed by .x (e.g. "0.4.x") in the table
            if not re.search(rf"\b{re.escape(minor_line)}\b", security_text):
                violations.append(
                    f"{security_md}: VERSION is {version_str} but no {minor_line}.x row found"
                    " in supported-versions table"
                )

    # --- 5. Removed auth mode ---
    # The legacy "dev" mode (without _token) was removed; only dev_token is valid.
    # Violation if README lists "dev" as a standalone auth mode label (e.g. "dev (local)").
    if readme.exists():
        readme_text = readme.read_text(encoding="utf-8")
        # Match "dev" preceded by a word boundary and followed by a space/paren (not "_token")
        for line_no, line in enumerate(readme_text.splitlines(), start=1):
            # Matches: "dev (local)", "`dev`", "| dev |" — but NOT "dev_token"
            if (re.search(r"\bdev\s*\(", line) or re.search(r"[`|]\s*dev\s*[`|]", line)) and "dev_token" not in line:
                violations.append(
                    f"{readme}:{line_no}: references removed 'dev' auth mode; current mode is 'dev_token'"
                )

    # --- 6. Auth modes documented AND implemented ---
    # The supported auth modes are a security contract. Catch drift in EITHER
    # direction: each canonical mode must be both documented on CLAUDE.md's
    # "Auth modes" line and referenced (as a quoted literal) in the server auth
    # source. Complements check #5 (which guards against the removed `dev`/`none`
    # modes reappearing).
    auth_modes = ("dev_token", "jwt", "header", "api_key", "webhook")
    server_auth_src = root / "packages/provide-uterm-server/src/provide/uterm/server"
    if claude_md.exists() and server_auth_src.is_dir():
        claude_text = claude_md.read_text(encoding="utf-8")
        auth_line = next((ln for ln in claude_text.splitlines() if "Auth modes:" in ln), "")
        src_blob = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in server_auth_src.rglob("*.py"))
        for mode in auth_modes:
            if f"`{mode}`" not in auth_line:
                violations.append(f"{claude_md}: 'Auth modes' line does not document the '{mode}' auth mode")
            if f'"{mode}"' not in src_blob and f"'{mode}'" not in src_blob:
                violations.append(
                    f"{server_auth_src}: auth mode '{mode}' is documented but not referenced in the server auth source"
                )

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
    violations.extend(_structural_claim_violations(root))
    violations.extend(_tool_count_violations(root))
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
