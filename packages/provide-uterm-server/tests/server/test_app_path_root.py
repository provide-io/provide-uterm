#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``ui.app_path = "/"`` mounts the UI at the site root.

``_clean_path`` has always accepted the root and kept it as ``"/"``, but two
things broke on it: FastAPI refuses a router prefix that ends in ``/`` (so the
app failed at startup with an AssertionError), and every link built as
``f"{app_path}/session/..."`` would begin ``//`` -- a protocol-relative URL
that points off-site. The C# port mounts the root through
``UiPaths.MountPrefix``, which turns it into an empty prefix; these tests pin
the same behaviour here.
"""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config


def _root_app():  # type: ignore[no-untyped-def]
    config = default_server_config()
    config.ui.app_path = "/"
    return config, create_server_app(config)


def _bootstrap(html: str) -> dict[str, object]:
    match = re.search(r"<script type='application/json' id='app-bootstrap'>(.*?)</script>", html, re.S)
    assert match is not None, html[:400]
    return json.loads(match.group(1))  # type: ignore[no-any-return]


def test_root_app_path_starts_and_serves_pages_at_the_root() -> None:
    _config, app = _root_app()
    with TestClient(app) as client:
        dashboard = client.get("/")
        replay = client.get("/replay/provide-shell")
    assert dashboard.status_code == 200
    assert replay.status_code == 200


def test_root_app_path_hands_the_frontend_an_empty_prefix() -> None:
    # The frontend builds links as `${app_path}/operator/...`; "/" there would
    # produce "//operator/...", a protocol-relative link to a host named
    # "operator".
    _config, app = _root_app()
    with TestClient(app) as client:
        dashboard = client.get("/")
    assert _bootstrap(dashboard.text)["app_path"] == ""


def test_root_app_path_short_link_redirect_stays_on_site() -> None:
    _config, app = _root_app()
    with TestClient(app) as client:
        r = client.get("/s/provide-shell", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/session/provide-shell"


def test_root_app_path_quick_connect_url_stays_on_site() -> None:
    _config, app = _root_app()
    with TestClient(app) as client:
        r = client.post("/api/connect", json={"connector_type": "shell"})
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("/session/"), url


def test_default_app_path_is_unchanged() -> None:
    config = default_server_config()
    app = create_server_app(config)
    with TestClient(app) as client:
        dashboard = client.get("/app/")
        r = client.get("/s/provide-shell", follow_redirects=False)
    assert _bootstrap(dashboard.text)["app_path"] == "/app"
    assert r.headers["location"] == "/app/session/provide-shell"
