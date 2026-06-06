#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Lazy-import behaviour of the ``provide.uterm.server`` package ``__init__``.

The Cloudflare Worker imports lightweight server submodules (e.g.
``provide.uterm.server.bridge.rest_helpers``) under Pyodide, where the FastAPI
app factory is not importable. So importing the ``provide.uterm.server`` package
must NOT eagerly pull in ``provide.uterm.server.app`` (the factory) — the public
names are resolved lazily via ``__getattr__`` instead.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_importing_server_package_does_not_import_the_app_factory() -> None:
    """A fresh ``import provide.uterm.server`` must not import the FastAPI app.

    Checked in a subprocess so the assertion is not polluted by other tests
    that have already imported ``provide.uterm.server.app`` in this process.
    """
    code = (
        "import sys; import provide.uterm.server; "
        "assert 'provide.uterm.server.app' not in sys.modules, "
        "sorted(m for m in sys.modules if m.startswith('provide.uterm.server'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_lazy_public_attributes_resolve() -> None:
    import provide.uterm.server as server_pkg

    assert callable(server_pkg.create_server_app)
    assert callable(server_pkg.config_from_mapping)
    assert callable(server_pkg.default_server_config)
    assert callable(server_pkg.load_server_config)


def test_unknown_attribute_raises_attribute_error() -> None:
    import provide.uterm.server as server_pkg

    with pytest.raises(AttributeError):
        _ = server_pkg.no_such_export
