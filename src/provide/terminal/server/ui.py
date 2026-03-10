#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""HTML helpers for the hosted server UI surfaces."""

from __future__ import annotations

import json
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def _shell(
    title: str,
    assets_path: str,
    body: str,
    *,
    extra_css: tuple[str, ...] = (),
    scripts: tuple[str, ...] = (),
    xterm_cdn: str = "",
    fonts_cdn: str = "",
) -> str:
    css_links = "".join(f"<link rel='stylesheet' href='{escape(assets_path)}/{escape(name)}'>" for name in extra_css)
    script_tags = "".join(
        f"<script type='module' src='{escape(assets_path)}/{escape(name)}'></script>" for name in scripts
    )
    xterm_css = f"<link rel='stylesheet' href='{escape(xterm_cdn)}/css/xterm.css'>" if xterm_cdn else ""
    fonts_link = f"<link href='{escape(fonts_cdn)}' rel='stylesheet'>" if fonts_cdn else ""
    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        f"<title>{escape(title)}</title>"
        f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-foundation.css'>"
        f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-layout.css'>"
        f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-components.css'>"
        f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-views.css'>"
        f"{css_links}{xterm_css}{fonts_link}"
        f"{body}{script_tags}</html>"
    )


def _bootstrap_tag(payload: Mapping[str, object]) -> str:
    blob = json.dumps(payload).replace("</", "<\\/")
    return f"<script type='application/json' id='app-bootstrap'>{blob}</script>"


def operator_dashboard_html(
    title: str, app_path: str, assets_path: str, xterm_cdn: str = "", fonts_cdn: str = ""
) -> str:
    bootstrap = {
        "page_kind": "dashboard",
        "title": title,
        "app_path": app_path,
        "assets_path": assets_path,
    }
    body = (
        "<body>"
        "<div id='app-root'></div>"
        "<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>"
        f"{_bootstrap_tag(bootstrap)}"
        "</body>"
    )
    return _shell(
        title, assets_path, body, scripts=("server-session-page.js",), xterm_cdn=xterm_cdn, fonts_cdn=fonts_cdn
    )


def session_page_html(
    title: str,
    assets_path: str,
    session_id: str,
    *,
    operator: bool,
    app_path: str,
    xterm_cdn: str = "",
    fonts_cdn: str = "",
) -> str:
    bootstrap = {
        "page_kind": "operator" if operator else "session",
        "title": title,
        "app_path": app_path,
        "assets_path": assets_path,
        "session_id": session_id,
        "surface": "operator" if operator else "user",
    }
    body = (
        "<body>"
        "<div id='app-root'></div>"
        "<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>"
        f"{_bootstrap_tag(bootstrap)}"
        f"<script src='{escape(assets_path)}/hijack.js'></script>"
        "</body>"
    )
    return _shell(
        title, assets_path, body, scripts=("server-session-page.js",), xterm_cdn=xterm_cdn, fonts_cdn=fonts_cdn
    )


def connect_page_html(title: str, assets_path: str, app_path: str, *, xterm_cdn: str = "", fonts_cdn: str = "") -> str:
    """Return a self-contained quick-connect form page."""
    safe_app = escape(app_path)
    # NOTE: /api/connect is mounted at root (not under app_path) so use the
    # absolute root path here, not f"{safe_app}/api/connect".
    inline_script = """
<script>
(function () {
  var form = document.getElementById('connect-form');
  var typeSelect = document.getElementById('connect-type');
  var errorBox = document.getElementById('connect-error');
  var submitBtn = document.getElementById('connect-submit');

  function updateFields() {
    var t = typeSelect.value;
    var sshFields = document.querySelectorAll('.field-ssh');
    var hostFields = document.querySelectorAll('.field-host');
    sshFields.forEach(function (el) { el.style.display = (t === 'ssh') ? '' : 'none'; });
    hostFields.forEach(function (el) { el.style.display = (t === 'ssh' || t === 'telnet') ? '' : 'none'; });
    var portEl = document.getElementById('connect-port');
    if (portEl && !portEl.dataset.userEdited) {
      portEl.value = t === 'telnet' ? '23' : '22';
    }
  }

  typeSelect.addEventListener('change', updateFields);
  document.getElementById('connect-port').addEventListener('input', function () {
    this.dataset.userEdited = '1';
  });
  updateFields();

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    errorBox.textContent = '';
    submitBtn.disabled = true;
    submitBtn.textContent = 'Connecting\u2026';
    var t = typeSelect.value;
    var payload = { connector_type: t };
    var name = document.getElementById('connect-name').value.trim();
    if (name) payload.display_name = name;
    if (t === 'ssh' || t === 'telnet') {
      payload.host = document.getElementById('connect-host').value.trim();
      payload.port = parseInt(document.getElementById('connect-port').value, 10) || (t === 'telnet' ? 23 : 22);
    }
    if (t === 'ssh') {
      var user = document.getElementById('connect-user').value.trim();
      var pass = document.getElementById('connect-pass').value;
      if (user) payload.username = user;
      if (pass) payload.password = pass;
    }
    fetch('/api/connect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (r) {
        if (!r.ok) { throw new Error(r.data.detail || 'Connection failed'); }
        window.location.href = r.data.url;
      })
      .catch(function (err) {
        errorBox.textContent = err.message;
        submitBtn.disabled = false;
        submitBtn.textContent = 'Connect';
      });
  });
})();
</script>"""
    field_css = (
        "<style>"
        ".field{margin-bottom:1rem}"
        ".field label{display:block;font-size:13px;color:var(--muted,#8a9bb0);margin-bottom:4px}"
        "</style>"
    )
    body = (
        "<body>"
        "<div class='page'>"
        "<div class='card' style='max-width:480px;margin:2rem auto'>"
        f"<div class='small' style='margin-bottom:.75rem'>"
        f"<a href='{safe_app}/'>← Dashboard</a>"
        "</div>"
        "<h2 style='margin-bottom:1.25rem'>Quick Connect</h2>"
        "<form id='connect-form'>"
        "<div class='field'>"
        "<label for='connect-type'>Connection type</label>"
        "<select id='connect-type' name='connector_type'>"
        "<option value='ssh'>SSH</option>"
        "<option value='telnet'>Telnet</option>"
        "<option value='shell'>Local Shell</option>"
        "</select>"
        "</div>"
        "<div class='field'>"
        "<label for='connect-name'>Display name (optional)</label>"
        "<input id='connect-name' type='text' placeholder='My session'>"
        "</div>"
        "<div class='field field-host'>"
        "<label for='connect-host'>Host</label>"
        "<input id='connect-host' type='text' placeholder='hostname or IP'>"
        "</div>"
        "<div class='field field-host'>"
        "<label for='connect-port'>Port</label>"
        "<input id='connect-port' type='number' value='22' min='1' max='65535'>"
        "</div>"
        "<div class='field field-ssh'>"
        "<label for='connect-user'>Username</label>"
        "<input id='connect-user' type='text' placeholder='username'>"
        "</div>"
        "<div class='field field-ssh'>"
        "<label for='connect-pass'>Password</label>"
        "<input id='connect-pass' type='password' placeholder='password'>"
        "</div>"
        "<div id='connect-error' style='color:var(--danger,#f66);margin:.5rem 0;font-size:13px'></div>"
        "<button id='connect-submit' class='btn primary' type='submit' style='width:100%'>Connect</button>"
        "</form>"
        "</div>"
        "</div>"
        f"{field_css}"
        f"{inline_script}"
        "</body>"
    )
    return _shell(title, assets_path, body, xterm_cdn=xterm_cdn, fonts_cdn=fonts_cdn)


def replay_page_html(
    title: str, assets_path: str, session_id: str, *, app_path: str, xterm_cdn: str = "", fonts_cdn: str = ""
) -> str:
    bootstrap = {
        "page_kind": "replay",
        "title": title,
        "app_path": app_path,
        "assets_path": assets_path,
        "session_id": session_id,
        "surface": "operator",
    }
    body = (
        "<body>"
        "<div id='app-root'></div>"
        "<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>"
        f"{_bootstrap_tag(bootstrap)}"
        "</body>"
    )
    return _shell(
        f"{title} Replay",
        assets_path,
        body,
        scripts=("server-replay-page.js",),
        xterm_cdn=xterm_cdn,
        fonts_cdn=fonts_cdn,
    )
