#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Every changed path is classified, and anything not provably irrelevant runs the perimeter.

An empty target list passes, so a change the gate misreads as "nothing to
mutate" is never checked. ``scripts/mutation_change_scope.py`` gives every
changed path one kind: a perimeter source runs its own target, a path on a
short commented allowlist runs nothing, and everything else -- support files,
tests, conftests, test data, off-perimeter source, dependency changes -- runs
the whole perimeter. These pin each kind, the allowlist's guard rails, the
real paths that earlier, narrowing versions sent to "nothing", and every path
main changed in the last 30 days.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import mutation_change_scope as scope
from scripts import run_mutation_gate

_AUTH = "src/provide/uterm/auth.py"


def _scope(
    changed: list[str], *, support: frozenset[str] = frozenset(), sources: dict[str, str] | None = None
) -> scope.Scope:
    return scope.change_scope(
        changed, is_support=lambda path: path in support, source_target=lambda path: (sources or {}).get(path)
    )


def _full(result: scope.Scope) -> list[str]:
    return [path for path, _reason in result.full]


class TestEveryKind:
    def test_a_perimeter_source_runs_its_own_target(self) -> None:
        path = "packages/provide-uterm/src/provide/uterm/auth.py"
        result = _scope([path], sources={path: _AUTH})
        assert (result.targets, result.full) == ([_AUTH], [])
        assert [c.kind for c in result.classified] == ["source"]

    def test_a_support_file_runs_the_whole_perimeter(self) -> None:
        result = _scope(["mutation_equivalents.toml"], support=frozenset({"mutation_equivalents.toml"}))
        assert _full(result) == ["mutation_equivalents.toml"]

    @pytest.mark.parametrize(
        "path",
        [
            # Each of these ran nothing under an earlier version that narrowed
            # tests and helpers to the targets a static reading said they reach.
            "packages/provide-uterm/conftest.py",
            "packages/provide-uterm/tests/bridge/testdata/behavior_vectors.json",
            "packages/provide-uterm-server/tests/conftest_part1.py",
            "packages/provide-uterm-server/tests/bridge/control_channel_helpers.py",
            "packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/__init__.py",
            # And these are the deny-by-default fallback.
            "packages/provide-uterm-server/tests/bridge/test_hub_polling_coverage.py",
            "packages/provide-uterm-server/src/provide/uterm/server/cli.py",
            "conformance/vectors/x.json",
            "spec/protocol.md",
            "uv.lock",
            "pyproject.toml",
            "VERSION",
            "a_new_top_level_file",
            "scripts/example_server.py",
        ],
    )
    def test_anything_not_allowlisted_runs_the_whole_perimeter(self, path: str) -> None:
        result = _scope([path])
        assert _full(result) == [path]
        assert [c.kind for c in result.classified] == ["python-side"]

    @pytest.mark.parametrize(
        "path",
        [
            "packages/provide-uterm-go/internal/term/screen.go",
            "packages/provide-uterm-ts/src/emulator.ts",
            "packages/provide-uterm-csharp/src/Provide.Uterm/Policy.cs",
            "packages/provide-uterm-cloudflare/src/worker.ts",
            "packages/provide-uterm-frontend/src/send-keys.ts",
            "docs/operations/runbook.md",
            ".github/workflows/codeql.yml",
            "scripts/demos/record_pty.py",
            "CHANGELOG.md",
            "package-lock.json",
        ],
    )
    def test_an_allowlisted_path_runs_nothing(self, path: str) -> None:
        result = _scope([path])
        assert (result.targets, result.full) == ([], [])
        assert [c.kind for c in result.classified] == ["irrelevant"]

    @pytest.mark.parametrize(
        "path",
        [
            "packages/provide-uterm-cloudflare/src/provide/uterm/cf/runtime.py",
            "packages/provide-uterm-cloudflare/a.toml",
        ],
    )
    def test_python_and_toml_in_the_cloudflare_package_are_not_allowlisted(self, path: str) -> None:
        assert _full(_scope([path])) == [path]

    def test_every_path_gets_exactly_one_kind_with_a_reason(self) -> None:
        changed = ["CHANGELOG.md", "uv.lock", "packages/provide-uterm-go/x.go"]
        result = _scope(changed)
        assert [c.path for c in result.classified] == changed
        assert all(c.kind in scope.KINDS and c.reason for c in result.classified)


class TestTheAllowlist:
    def test_every_entry_says_why(self) -> None:
        assert scope.IRRELEVANT
        for entry in scope.IRRELEVANT:
            assert len(entry.reason.split()) >= 8, entry

    @pytest.mark.parametrize(
        "path",
        [
            "packages/provide-uterm/src/provide/uterm/emulator.py",
            "packages/provide-uterm-server/tests/conftest.py",
            "packages/provide-uterm-platform/tests/pty/testdata/behavior_vectors.json",
            "packages/provide-uterm-client/src/provide/uterm/client/x.py",
            "packages/provide-uterm-annotation/pyproject.toml",
            "conftest.py",
            "hypothesis_profiles.py",
            "pyproject.toml",
        ],
    )
    def test_nothing_python_side_is_ever_allowlisted(self, path: str) -> None:
        assert scope.irrelevant_entry(path) is None

    def test_a_script_a_conftest_loads_by_path_is_not_allowlisted(self) -> None:
        # Both conftest_part1.py files load scripts/example_server.py with
        # spec_from_file_location, so scripts/ is not wholly unreadable.
        text = (_ROOT / "packages/provide-uterm-server/tests/conftest_part1.py").read_text(encoding="utf-8")
        assert "example_server.py" in text
        assert scope.irrelevant_entry("scripts/example_server.py") is None

    def test_a_config_a_bound_test_loads_is_not_allowlisted(self) -> None:
        # test_config.py loads the checked-in docker/server.toml and asserts it
        # fails closed; editing it can break or hollow out that kill suite.
        text = (_ROOT / "packages/provide-uterm-server/tests/server/test_config.py").read_text(encoding="utf-8")
        assert '"docker" / "server.toml"' in text
        assert scope.irrelevant_entry("docker/server.toml") is None
        assert scope.irrelevant_entry("docker/Dockerfile.server") is not None

    def test_a_corpus_a_python_test_reads_from_the_go_port_is_not_allowlisted(self) -> None:
        # test_capture_record.py checks the Python reader against the Go port's
        # capture-record corpus, so editing that corpus can change its kills.
        text = (_ROOT / "packages/provide-uterm-server/tests/server/test_capture_record.py").read_text(encoding="utf-8")
        assert "capture_record_golden.json" in text
        corpus = "packages/provide-uterm-go/capturestate/testdata/capture_record_golden.json"
        assert scope.irrelevant_entry(corpus) is None
        assert scope.irrelevant_entry("packages/provide-uterm-go/capturestate/store.go") is not None

    def test_no_test_side_file_reads_into_an_allowlisted_area(self) -> None:
        """A literal repo path in test-side code must not point at something the allowlist skips.

        The allowlist is a claim that no Python test reads these paths. This
        checks the claim against every bound test, every conftest and every
        non-test module in a Python-side test tree, so a new test that reads an
        allowlisted file fails the build here instead of quietly running no
        mutation for edits to that file. The known reads are listed with why
        each is safe; a stale entry fails too.

        It is a backstop for literal reads, not proof that nothing reads these
        paths: a path computed from data, an environment variable or a config
        file is invisible to it. The allowlist's reasons are the real claim.
        """
        found = _literal_reads_into_allowlisted_areas()
        assert found == _REVIEWED_READS, (
            "a test-side file reads a path the allowlist calls irrelevant; exclude that path from its "
            f"entry, or list the read in _REVIEWED_READS with why it is safe: {sorted(found ^ _REVIEWED_READS)}"
        )

    @pytest.mark.parametrize("path", ["conftest.py", "hypothesis_profiles.py"])
    def test_the_pytest_bootstrap_is_a_support_file(self, path: str) -> None:
        assert run_mutation_gate._changed_mutation_support_paths([path]) == [path]


#: Literal reads into allowlisted areas that are safe, and why. Keyed by
#: (reading file, path it names).
_REVIEWED = {
    ("conftest.py", "packages/provide-uterm-cloudflare/src"): (
        "puts the Worker's Python on sys.path; .py, .pyi and .toml are excluded from the cloudflare entry"
    ),
    ("packages/provide-uterm/tests/playwright/backend_server.py", "packages/provide-uterm-go"): (
        "the playwright backend matrix, which only the unbound playwright suite loads"
    ),
    (
        "packages/provide-uterm/tests/playwright/backend_server.py",
        "packages/provide-uterm-csharp/cmd/Uterm/Uterm.csproj",
    ): ("the playwright backend matrix, which only the unbound playwright suite loads"),
}
_REVIEWED_READS = frozenset(_REVIEWED)


_WILDCARDS = re.compile(r"[*?\[]")
_PATH_FUNCTIONS = frozenset({"join", "Path", "PurePath", "PosixPath", "joinpath"})
_GLOB_FUNCTIONS = frozenset({"glob", "rglob", "iglob"})


def _constant(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _runs(parts: list[ast.expr]) -> list[tuple[str, bool]]:
    """The constant stretches of a path built from *parts*, each with whether it starts the path.

    ``root / DOCKER / "server.toml"`` has one stretch, ``server.toml``, that
    does not start the path: a variable comes before it. Both kinds are
    checked; the second only by matching the end of an allowlisted path.
    """
    runs: list[tuple[str, bool]] = []
    current: list[str] = []
    anchored = True
    for part in parts:
        value = _constant(part)
        if value is None:
            if current:
                runs.append(("/".join(current), anchored))
            current, anchored = [], False
        else:
            current.append(value)
    if current:
        runs.append(("/".join(current), anchored))
    return runs


def _division_operands(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [*_division_operands(node.left), *_division_operands(node.right)]
    return [node]


def _called(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


#: Bases that are pytest's per-test temporary directories, never the repo.
_TEMPORARY_BASES = frozenset({"tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory"})

#: Calls that read or open the path they are given, for a loose file name.
_READ_CALLS = frozenset(
    {"open", "Path", "PurePath", "PosixPath", "read_text", "read_bytes", "exists", "is_file", "is_dir", "stat", "load"}
)


def _base_name(node: ast.expr) -> str | None:
    """The variable a path expression is built on: ``tmp_path`` in ``tmp_path.joinpath("a")``."""
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func.value if isinstance(node.func, ast.Attribute) else (node.args[0] if node.args else node)
            if isinstance(node, ast.Call) and not node.args and not isinstance(node.func, ast.Attribute):
                return None
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            node = node.left
        else:
            return None


class _FileRelative:
    """Resolve ``Path(__file__)...`` bases to a repo-relative directory of the reading module.

    ``here = Path(__file__).resolve()`` then ``here.parent / "testdata"`` is
    the reader's own ``testdata``. Simple assignments are followed (a name
    assigned more than one way is not), and a base that climbs above the
    repo root -- the ``mutants/`` copies -- resolves to nothing.
    """

    def __init__(self, tree: ast.AST, reader: str) -> None:
        self._reader = reader
        values: dict[str, list[ast.expr]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                values.setdefault(node.targets[0].id, []).append(node.value)
        self._names = {name: exprs[0] for name, exprs in values.items() if len(exprs) == 1}

    def levels(self, node: ast.expr, depth: int = 0) -> int | None:
        """How many directories above the reading file *node* is, or None if it is not ``__file__``-based."""
        if depth > 20 or not self._reader:
            return None
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return 0
            target = self._names.get(node.id)
            return self.levels(target, depth + 1) if target is not None else None
        if isinstance(node, ast.Call):
            called = _called(node)
            if called in {"Path", "PurePath", "PosixPath"} and len(node.args) == 1:
                return self.levels(node.args[0], depth + 1)
            if called in {"resolve", "absolute"} and isinstance(node.func, ast.Attribute):
                return self.levels(node.func.value, depth + 1)
            if called == "dirname" and len(node.args) == 1:
                inner = self.levels(node.args[0], depth + 1)
                return None if inner is None else inner + 1
            return None
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            inner = self.levels(node.value, depth + 1)
            return None if inner is None else inner + 1
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, int):
                inner = self.levels(node.value.value, depth + 1)
                return None if inner is None else inner + index.value + 1
        return None

    def directory(self, node: ast.expr) -> str | None:
        """The repo-relative directory *node* names ("" for the repo root), or None."""
        levels = self.levels(node)
        parts = self._reader.split("/")
        if levels is None or levels == 0 or levels > len(parts):
            return None
        return "/".join(parts[: len(parts) - levels])


def _literal_paths(tree: ast.AST, reader: str = "") -> set[tuple[str, bool]]:
    """Paths a module spells out, as ``(path, starts the path)`` pairs.

    Covers ``"a/b"`` strings, ``x / "a" / "b"`` chains, ``os.path.join``,
    ``Path(...)`` and ``.joinpath(...)`` with constant arguments, bare file
    names such as ``"README.md"`` passed to a read, and glob patterns, which
    count up to their first wildcard. A chain broken by a variable is split
    into the constant stretches on either side of it. A chain built on the
    reading module's own ``__file__`` resolves to the exact repo path. Left
    out: route-like strings that start with ``/``, and paths built on pytest's
    temporary directories.
    """
    found: set[tuple[str, bool]] = set()
    resolver = _FileRelative(tree, reader)
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    # Constants already read as part of a chain or a path call; ast.walk visits
    # a parent before its children, so they are known by the time they come up.
    consumed: set[int] = set()

    def add_path(parts: list[ast.expr]) -> None:
        consumed.update(id(child) for part in parts for child in ast.walk(part))
        if not parts or _base_name(parts[0]) in _TEMPORARY_BASES:
            return
        base = resolver.directory(parts[0])
        runs = _runs(parts)
        if base is not None and len(parts) > 1 and _constant(parts[1]) is not None:
            # The first stretch continues the reader's own directory: exact.
            first, _anchored = runs[0]
            runs[0] = (f"{base}/{first}" if base else first, True)
        found.update(runs)

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and id(node) not in consumed:
            consumed.update(id(child) for child in ast.walk(node))
            add_path(_division_operands(node))
        elif isinstance(node, ast.Call):
            called = _called(node)
            if called in _PATH_FUNCTIONS:
                receiver = [node.func.value] if isinstance(node.func, ast.Attribute) and called == "joinpath" else []
                add_path([*receiver, *node.args])
            elif called in _GLOB_FUNCTIONS and node.args and (pattern := _constant(node.args[0])):
                consumed.add(id(node.args[0]))
                receiver_node = node.func.value if isinstance(node.func, ast.Attribute) else None
                if receiver_node is not None and called != "iglob":
                    if _base_name(receiver_node) in _TEMPORARY_BASES:
                        continue
                    # `(root / "docs").glob("**/*.md")`: the pattern continues
                    # the receiver's path, so join it to the receiver's last
                    # constant stretch when the receiver ends in one.
                    runs = _runs(_division_operands(receiver_node))
                    if runs and isinstance(receiver_node, ast.BinOp) and _constant(receiver_node.right) is not None:
                        stem, anchored = runs[-1]
                        found.add((f"{stem}/{pattern}", anchored))
                    else:
                        found.add((pattern, False))
                else:
                    found.add((pattern, True))
        elif id(node) not in consumed and (value := _constant(node)) is not None:
            if any(ch.isspace() for ch in value) or value.startswith("/"):
                continue  # prose, or a route such as "/docs"
            parent = parents.get(id(node))
            # A bare file name is a read only when it is handed to one.
            handed_to_read = isinstance(parent, ast.Call) and _called(parent) in _READ_CALLS
            if "/" in value or (_is_allowlisted_file_name(value) and handed_to_read):
                found.add((value, True))
    cleaned: set[tuple[str, bool]] = set()
    for path, anchored in found:
        wildcard = _WILDCARDS.search(path)
        if wildcard:
            path = path[: wildcard.start()].rpartition("/")[0]
        path = path.strip("/")
        if path:
            cleaned.add((path, anchored))
    return cleaned


def _is_allowlisted_file_name(value: str) -> bool:
    entry = scope.irrelevant_entry(value)
    return entry is not None and not entry.prefix.endswith("/")


def _allowlisted_files() -> list[str]:
    listed = subprocess.run(["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True, check=True).stdout
    return [path for path in listed.splitlines() if scope.irrelevant_entry(path) is not None]


def _points_into_allowlist(path: str, anchored: bool, allowlisted: list[str]) -> bool:
    if not anchored:
        # After a variable only the end of the path is known, so any
        # allowlisted file with that ending counts. A chain built on the
        # reader's own __file__ is resolved exactly before it gets here.
        if any(f == path or f.endswith("/" + path) for f in allowlisted):
            return True
        # As a directory, a lone name like "src" or "tests" is in every
        # package, so it counts only if it names an allowlisted top-level
        # directory ("docs", "docker"); two or more segments are specific
        # enough to mean one place.
        if "/" not in path:
            return any(entry.prefix == path + "/" for entry in scope.IRRELEVANT)
        return any(f.startswith(path + "/") or f"/{path}/" in f for f in allowlisted)
    if scope.irrelevant_entry(path) is not None and (_ROOT / path).exists():
        return True
    return (_ROOT / path).is_dir() and any(
        entry.prefix.endswith("/") and (path + "/").startswith(entry.prefix) for entry in scope.IRRELEVANT
    )


def _literal_reads_into_allowlisted_areas() -> frozenset[tuple[str, str]]:
    selection = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"][
        "pytest_add_cli_args_test_selection"
    ]
    listed = subprocess.run(
        ["git", "ls-files", "--", "*.py"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    test_side = {
        *selection,
        *(path for path in listed if Path(path).name == "conftest.py"),
        *(
            path
            for path in listed
            if scope.is_python_side(path)
            and "tests" in Path(path).parts[:-1]
            and not Path(path).name.startswith("test_")
        ),
    }
    allowlisted = _allowlisted_files()
    reads: set[tuple[str, str]] = set()
    for reader in sorted(test_side):
        for path, anchored in _literal_paths(ast.parse((_ROOT / reader).read_text(encoding="utf-8")), reader):
            if _points_into_allowlist(path, anchored, allowlisted):
                reads.add((reader, path))
    return frozenset(reads)


def _scan(source: str, reader: str = "") -> set[tuple[str, bool]]:
    return _literal_paths(ast.parse(source), reader)


class TestThePathScan:
    """The scan is a backstop for literal reads, not proof that nothing reads these paths."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ('root / "docker" / "server.toml"', ("docker/server.toml", False)),
            ('"docker" / Path("server.toml")', ("docker", True)),
            ('os.path.join(root, "docker", "server.toml")', ("docker/server.toml", False)),
            ('os.path.join("docker", "server.toml")', ("docker/server.toml", True)),
            ('Path("docker", "server.toml")', ("docker/server.toml", True)),
            ('root.joinpath("docker", "server.toml")', ("docker/server.toml", False)),
            ('open("README.md")', ("README.md", True)),
            ('root / DOCKER / "server.toml"', ("server.toml", False)),
            ('root.glob("docker/*.toml")', ("docker", False)),
            ('glob.iglob("docs/**/*.md")', ("docs", True)),
        ],
        ids=[
            "chain",
            "chain-into-call",
            "os-path-join",
            "os-path-join-anchored",
            "path-args",
            "joinpath",
            "bare-name",
            "broken-chain",
            "glob-method",
            "glob-function",
        ],
    )
    def test_every_way_of_spelling_a_path_is_seen(self, source: str, expected: tuple[str, bool]) -> None:
        assert expected in _scan(source)

    @pytest.mark.parametrize(
        "source",
        [
            'root / "docker" / "Dockerfile.server"',
            'os.path.join(root, "docker", "Dockerfile.server")',
            'Path(root, "docker", "Dockerfile.server")',
            'root.joinpath("docker", "Dockerfile.server")',
            'open("README.md")',
            'root / DOCKER / "Dockerfile.server"',
            'root.glob("docker/Dockerfile.*")',
            'sorted((root / "docs").glob("**/*.md"))',
        ],
        ids=["chain", "os-path-join", "path-args", "joinpath", "bare-name", "broken-chain", "glob", "dir-then-glob"],
    )
    def test_each_form_reading_an_allowlisted_file_is_flagged(self, source: str) -> None:
        # End to end: whatever the spelling, reading an allowlisted file is a read.
        allowlisted = _allowlisted_files()
        assert any(_points_into_allowlist(path, anchored, allowlisted) for path, anchored in _scan(source))

    _SERVER_TEST = "packages/provide-uterm-server/tests/server/test_x.py"

    def _flagged(self, source: str, reader: str = _SERVER_TEST) -> bool:
        allowlisted = _allowlisted_files()
        return any(_points_into_allowlist(path, anchored, allowlisted) for path, anchored in _scan(source, reader))

    @pytest.mark.parametrize(
        "source",
        [
            'client.get("/docs")',
            'tmp_path / "docs"',
            '(tmp_path / "package.json").write_text("{}")',
            'tmp_path.joinpath("docs", "x.md")',
            'tmp_path_factory.mktemp("d") / "README.md"',
            'config = {"name": "Makefile"}',
        ],
        ids=["route", "tmp-dir", "tmp-file", "tmp-joinpath", "tmp-factory", "dict-value"],
    )
    def test_routes_temp_paths_and_data_are_not_reads(self, source: str) -> None:
        assert not self._flagged(source)

    def test_a_file_name_handed_to_open_is_a_read(self) -> None:
        assert self._flagged('open("Makefile")')

    def test_a_repo_root_read_is_flagged_even_if_the_package_has_a_namesake(self) -> None:
        # The core package has its own README.md; repo_root / "README.md"
        # still names the top-level one, which the allowlist skips.
        reader = "packages/provide-uterm/tests/test_x.py"
        assert (_ROOT / "packages/provide-uterm/README.md").exists()
        assert self._flagged('repo_root / "README.md"', reader)

    def test_a_chain_on_the_readers_own_file_resolves_exactly(self) -> None:
        platform_test = "packages/provide-uterm-platform/tests/pty/test_x.py"
        own = 'Path(__file__).resolve().parent / "testdata" / "behavior_vectors.json"'
        assert ("packages/provide-uterm-platform/tests/pty/testdata/behavior_vectors.json", True) in _scan(
            own, platform_test
        )
        assert not self._flagged(own, platform_test)
        # Climbing to the repo root resolves there, and docker/ is allowlisted.
        root = 'Path(__file__).resolve().parents[4] / "docker" / "Dockerfile.server"'
        assert ("docker/Dockerfile.server", True) in _scan(root, self._SERVER_TEST)
        assert self._flagged(root)

    def test_a_name_bound_to_the_readers_file_is_followed(self) -> None:
        source = 'here = Path(__file__).resolve()\nvectors = here.parent / "testdata" / "behavior_vectors.json"\n'
        reader = "packages/provide-uterm/tests/bridge/test_x.py"
        assert ("packages/provide-uterm/tests/bridge/testdata/behavior_vectors.json", True) in _scan(source, reader)

    def test_a_broken_chain_keeps_the_stretch_before_the_variable(self) -> None:
        assert ("docs/guides", True) in _scan('"docs" / "guides" / name')

    @pytest.mark.parametrize(
        ("path", "anchored"),
        [("README.md", True), ("docs", True), ("Makefile.bench", True), ("runbook.md", False)],
    )
    def test_a_read_of_an_allowlisted_path_is_caught(self, path: str, anchored: bool) -> None:
        assert _points_into_allowlist(path, anchored, _allowlisted_files())

    @pytest.mark.parametrize(
        ("path", "anchored"), [("docker/server.toml", True), ("scripts/example_server.py", True), ("x.json", False)]
    )
    def test_a_read_of_a_path_kept_off_the_allowlist_is_not(self, path: str, anchored: bool) -> None:
        assert not _points_into_allowlist(path, anchored, _allowlisted_files())


class TestTheGate:
    def _emit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], changed: list[str]
    ) -> list[str]:
        monkeypatch.setattr(run_mutation_gate, "_changed_python_paths", lambda *_a, **_k: [])
        monkeypatch.setattr(run_mutation_gate, "_changed_paths", lambda *_a, **_k: list(changed))
        monkeypatch.setattr(
            sys, "argv", ["run_mutation_gate.py", "--changed-only", "--emit-targets", "--base-ref", "HEAD"]
        )
        assert run_mutation_gate.main() == 0
        return capsys.readouterr().out.splitlines()

    def test_a_bound_test_change_runs_the_whole_perimeter(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._emit(monkeypatch, capsys, ["packages/provide-uterm-server/tests/bridge/hub/test_polling_kill.py"])
        assert run_mutation_gate.FULL_PERIMETER_REQUIRED_MARKER in out

    def test_an_allowlisted_change_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._emit(monkeypatch, capsys, ["docs/operations/runbook.md", "packages/provide-uterm-go/go.mod"])
        assert run_mutation_gate.FULL_PERIMETER_REQUIRED_MARKER not in out
        assert f"{run_mutation_gate.TARGETS_LINE_PREFIX}[]" in out


def _paths_changed_on_main_in_30_days() -> list[str]:
    result = subprocess.run(
        ["git", "log", "--first-parent", "--since=30 days ago", "--name-only", "--format=", "HEAD"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return sorted({line for line in result.stdout.splitlines() if line})


class TestThirtyDaysOfMain:
    def test_every_path_changed_on_main_is_classified(self) -> None:
        # CI checks this repo out at depth 1, where there is no history to
        # walk; the classification itself is pinned by the tests above.
        paths = _paths_changed_on_main_in_30_days()
        if len(paths) < 50:
            pytest.skip("shallow clone: no 30-day history to classify")
        classified = [
            scope.classify(path, is_support=lambda _p: False, source_target=lambda _p: None) for path in paths
        ]
        assert [c.path for c in classified] == paths
        assert all(c.kind in scope.KINDS and c.reason for c in classified)
        leaked = [c.path for c in classified if c.kind == "irrelevant" and scope.is_python_side(c.path)]
        assert not leaked, f"allowlisted, but on the Python side: {leaked}"
