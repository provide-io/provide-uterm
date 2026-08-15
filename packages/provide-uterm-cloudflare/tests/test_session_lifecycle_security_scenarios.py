# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native Cloudflare adapter for the shared lifecycle security contract.

This is intentionally a real public-route test.  It generates the Python
Worker vendor tree, boots ``pywrangler dev`` under Node 22, serves a local
JWKS, and drives workerd over HTTP and WebSocket connections.  A missing tool
or failed runtime is a test failure, never a skip.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import importlib.metadata
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cf_jwt_harness import AUDIENCE, ISSUER, build_keypair, mint
from cf_lifecycle_ws import (
    WORKER_TOKEN,
    acquire,
    collect_matching,
    connect,
    drain_browser_startup,
    drain_worker_startup,
    headers,
    matching_count,
    observation,
    receive_matching,
    send_control,
    ws_url,
)

from provide.uterm.control_channel import encode_control_frame

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
CONTRACT_PATH = Path(
    os.environ.get(
        "SESSION_LIFECYCLE_SCENARIO_CONTRACT",
        REPO_ROOT / "spec/session_lifecycle_security_scenarios.json",
    )
)
OUTPUT_PATH = os.environ.get("SESSION_LIFECYCLE_SCENARIO_OUTPUT")
HIBERNATION_IDLE_S = 40


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _node22_bin() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("NODE22_BIN")
    if configured:
        configured_path = Path(configured)
        candidates.append(configured_path / "node" if configured_path.is_dir() else configured_path)
    current = shutil.which("node")
    if current:
        candidates.append(Path(current))
    candidates.extend(
        [
            Path("/opt/homebrew/opt/node@22/bin/node"),
            Path("/usr/local/opt/node@22/bin/node"),
        ]
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        result = subprocess.run([str(candidate), "--version"], capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip().startswith("v22."):
            return candidate.parent
    raise AssertionError("Node 22 is required for the native Cloudflare lifecycle adapter")


def _locked_pywrangler() -> Path:
    """Return the workers-py script from this lockfile-provisioned environment."""
    installed = importlib.metadata.version("workers-py")
    lock_text = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert f'name = "workers-py"\nversion = "{installed}"' in lock_text, (
        f"installed workers-py {installed} isn't selected by uv.lock"
    )
    executable = Path(sys.executable).parent / "pywrangler"
    assert executable.is_file(), (
        "pywrangler must be provisioned beside the active Python executable; "
        "run uv sync --frozen --package provide-uterm-cloudflare --extra dev"
    )
    return executable


def _selected_uv() -> Path:
    """Resolve the explicitly provisioned uv binary used by pywrangler."""
    executable = shutil.which("uv")
    assert executable is not None, "uv is required to provision pywrangler's Python modules"
    return Path(executable).resolve()


def _prepare_worker_vendor_tree() -> None:
    """Generate the ignored module tree without consulting published wheels."""
    vendor_provide = PACKAGE_ROOT / "python_modules" / "provide"
    vendor_uterm = vendor_provide / "uterm"
    copy_options = {"dirs_exist_ok": True, "ignore": shutil.ignore_patterns("__pycache__", "*.pyc")}
    shutil.copytree(REPO_ROOT / "packages/provide-uterm/src/provide/uterm", vendor_uterm, **copy_options)
    shutil.copytree(REPO_ROOT / "packages/provide-uterm-server/src/provide/uterm", vendor_uterm, **copy_options)
    # A regular package in python_modules shadows the Worker source namespace;
    # mirror the Cloudflare implementation into that same package tree.
    shutil.copytree(PACKAGE_ROOT / "src/provide/uterm/cloudflare", vendor_uterm / "cloudflare", **copy_options)
    vendor_provide.mkdir(parents=True, exist_ok=True)
    (vendor_provide / "__init__.py").write_text('"""Vendored provide namespace."""\n', encoding="utf-8")
    (vendor_uterm / "__init__.py").write_text(
        '"""Minimal package initializer for the Cloudflare Python runtime."""\n',
        encoding="utf-8",
    )
    telemetry = vendor_provide / "telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    telemetry.joinpath("__init__.py").write_text(
        "class _Span:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *_args): return False\n"
        "class _Tracer:\n"
        "    def start_as_current_span(self, _name): return _Span()\n"
        "def get_tracer(_name): return _Tracer()\n"
        "def get_logger(_name):\n"
        "    import logging\n"
        "    return logging.getLogger(_name)\n",
        encoding="utf-8",
    )
    (PACKAGE_ROOT / ".venv-workers").mkdir(exist_ok=True)
    workers_py_version = importlib.metadata.version("workers-py")
    (PACKAGE_ROOT / ".venv-workers/.synced").write_text(workers_py_version, encoding="utf-8")
    (PACKAGE_ROOT / "python_modules/.synced").write_text(workers_py_version, encoding="utf-8")


@contextmanager
def _jwks_server(jwks: dict[str, Any]) -> Iterator[str]:
    body = json.dumps(jwks).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/jwks"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _health_ready(base_url: str, process: subprocess.Popen[str], timeout_s: float = 120) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    return False


@contextmanager
def _worker_server(jwks_url: str, *, resume_enabled: bool = True) -> Iterator[str]:
    _prepare_worker_vendor_tree()
    pywrangler = _locked_pywrangler()
    uv = _selected_uv()
    node_bin = _node22_bin()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    command = [
        str(pywrangler),
        "dev",
        "--port",
        str(port),
        "--ip",
        "127.0.0.1",
        "--var",
        "ENVIRONMENT:development",
        "--var",
        "AUTH_MODE:jwt",
        "--var",
        f"JWT_JWKS_URL:{jwks_url}",
        "--var",
        f"JWT_ISSUER:{ISSUER}",
        "--var",
        f"JWT_AUDIENCE:{AUDIENCE}",
        "--var",
        "JWT_ALGORITHMS:RS256",
        "--var",
        "HIJACK_LEASE_S:300",
        "--var",
        f"RESUME_ENABLED:{str(resume_enabled).lower()}",
        "--var",
        f"WORKER_BEARER_TOKEN:{WORKER_TOKEN}",
    ]
    with (
        tempfile.TemporaryDirectory(prefix="uterm-cf-tools-") as tool_dir,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log,
    ):
        # Expose only the selected uv executable to pywrangler; do not inherit
        # user/global bin directories where an arbitrary pywrangler could win.
        Path(tool_dir, "uv").symlink_to(uv)
        environment = {**os.environ, "PATH": os.pathsep.join((str(node_bin), tool_dir, "/usr/bin", "/bin"))}
        process = subprocess.Popen(
            command,
            cwd=PACKAGE_ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            if not _health_ready(base_url, process):
                log.seek(0)
                tail = log.read()[-12_000:]
                raise AssertionError(f"pywrangler dev failed to become healthy:\n{tail}")
            try:
                yield base_url
            except BaseException as exc:
                log.flush()
                log.seek(0)
                tail = log.read()[-12_000:]
                response = getattr(exc, "response", None)
                response_body = getattr(response, "body", b"")
                raise AssertionError(
                    f"native Cloudflare lifecycle request failed: {exc!r}\nresponse body: {response_body!r}\n{tail}"
                ) from exc
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


async def _fragments(
    payload: str,
    count: int,
    started: asyncio.Event,
    finish: asyncio.Event,
) -> AsyncIterator[str]:
    width = max(1, len(payload) // count)
    parts = [payload[index : index + width] for index in range(0, len(payload), width)]
    while len(parts) > count:
        parts[-2:] = [parts[-2] + parts[-1]]
    yield parts[0]
    started.set()
    await finish.wait()
    for part in parts[1:]:
        yield part


async def _fragment_counts(sender: Any, payload: str, count: int, receiver: Any, predicate: Any) -> tuple[int, int]:
    started = asyncio.Event()
    finish = asyncio.Event()
    task = asyncio.create_task(sender.send(_fragments(payload, count, started, finish)))
    await asyncio.wait_for(started.wait(), timeout=2)
    before = await matching_count(receiver, predicate)
    finish.set()
    await asyncio.wait_for(task, timeout=4)
    await receive_matching(receiver, predicate)
    return before, 1 + await matching_count(receiver, predicate)


async def _fragmentation(base: str, token: str, scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    data = scenario["input"]
    worker_id = data["worker_id"]
    payload = data["payload"]
    count = data["fragment_count"]
    worker_url = ws_url(base, f"/ws/worker/{worker_id}/term")
    browser_url = ws_url(base, f"/ws/browser/{worker_id}/term")

    def is_payload(event: dict[str, Any]) -> bool:
        return event.get("type") == "data" and event.get("data") == payload

    async with (
        connect(worker_url, additional_headers=headers(WORKER_TOKEN)) as worker,
        connect(browser_url, additional_headers=headers(token)) as browser,
    ):
        await drain_worker_startup(worker)
        await drain_browser_startup(browser)
        if data["transport"] == "browser":
            await acquire(browser, worker)
            wire_payload = encode_control_frame({"type": "input", "data": payload})
            before, after = await _fragment_counts(browser, wire_payload, count, worker, is_payload)
            await send_control(browser, {"type": "input", "data": "X" * data["oversized_bytes"]})
            error = await receive_matching(browser, lambda event: event.get("type") == "error")
            oversized_refused = "too large" in str(error.get("message"))
            route = "browser_websocket"
        else:
            before, after = await _fragment_counts(worker, payload, count, browser, is_payload)
            await worker.send("X" * data["oversized_bytes"])
            error = await receive_matching(worker, lambda event: event.get("type") == "error")
            oversized_refused = "too large" in str(error.get("message"))
            route = "worker_websocket"
    return observation(
        scenario,
        defaults,
        route=route,
        status_code=101,
        fragment_count=count,
        pre_final_actions=before,
        post_final_actions=after,
        oversized_refused=oversized_refused,
        delivered_payloads=[payload],
    )


def _unsupported(base: str, token: str, scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    operation = scenario["input"]["operation"]
    suffix = "browser-quota" if operation == "browser_quota" else "governance"
    request = urllib.request.Request(  # noqa: S310 - base is the locally created loopback server
        f"{base}/api/lifecycle/{suffix}", headers=headers(token)
    )
    try:
        urllib.request.urlopen(request, timeout=5)  # noqa: S310
    except urllib.error.HTTPError as response:
        body = json.loads(response.read())
        status = response.code
    else:  # pragma: no cover - a false capability must fail loudly
        raise AssertionError(f"Cloudflare lifecycle {suffix} route did not refuse the request")
    values: dict[str, Any] = {"route": "http", "status_code": status, "error": body["error"]}
    if operation == "governed_input":
        values["policy_decision"] = "unsupported"
    return observation(scenario, defaults, **values)


async def _resume(
    base: str, tokens: dict[str, str], scenario: dict[str, Any], defaults: dict[str, Any]
) -> dict[str, Any]:
    data = scenario["input"]
    worker_id = data["worker_id"]
    worker_url = ws_url(base, f"/ws/worker/{worker_id}/term")
    browser_url = ws_url(base, f"/ws/browser/{worker_id}/term")
    restored = replay_rejected = competing_preserved = False
    async with connect(worker_url, additional_headers=headers(WORKER_TOKEN)) as worker:
        await drain_worker_startup(worker)
        original = await connect(browser_url, additional_headers=headers(tokens[data["principal"]]))
        hello = await drain_browser_startup(original)
        resume_token = hello["resume_token"]
        await acquire(original, worker)
        await original.close()
        await receive_matching(worker, lambda event: event.get("type") == "control" and event.get("action") == "resume")
        if data["case"] == "current_owner":
            resumed = await connect(browser_url, additional_headers=headers(tokens[data["principal"]]))
            await drain_browser_startup(resumed)
            await send_control(resumed, {"type": "resume", "token": resume_token})
            resumed_hello = await receive_matching(
                resumed, lambda event: event.get("type") == "hello" and event.get("resumed") is True
            )
            await receive_matching(
                worker, lambda event: event.get("type") == "control" and event.get("action") == "pause"
            )
            await send_control(resumed, {"type": "input", "data": "resume-proof"})
            restored = bool(resumed_hello["resumed"]) and bool(
                await receive_matching(
                    worker, lambda event: event.get("type") == "data" and event.get("data") == "resume-proof"
                )
            )
            replay = await connect(browser_url, additional_headers=headers(tokens[data["principal"]]))
            await drain_browser_startup(replay)
            await send_control(replay, {"type": "resume", "token": resume_token})
            replay_rejected = (
                await matching_count(
                    replay,
                    lambda event: event.get("type") == "hello" and event.get("resumed") is True,
                    timeout=0.4,
                )
                == 0
            )
            await send_control(resumed, {"type": "ping"})
            warm_witness = await receive_matching(
                resumed,
                lambda event: event.get("type") == "heartbeat" and "runtime_activation_seq" in event,
            )
            # A locally booted Durable Object hibernates after an idle window;
            # the original edge-held browser and worker attachments must wake
            # the cold isolate and recover ownership/generation state.
            await asyncio.sleep(HIBERNATION_IDLE_S)
            await send_control(resumed, {"type": "ping"})
            cold_witness = await receive_matching(
                resumed,
                lambda event: event.get("type") == "heartbeat" and "runtime_activation_seq" in event,
                timeout=8,
            )
            assert cold_witness["runtime_activation_seq"] > warm_witness["runtime_activation_seq"]
            assert cold_witness["runtime_incarnation"] != warm_witness["runtime_incarnation"]
            await send_control(worker, {"type": "ping"})
            await receive_matching(resumed, lambda event: event.get("type") == "ping", timeout=8)
            await send_control(resumed, {"type": "input", "data": "post-hibernation-proof"})
            post_wake = await receive_matching(
                worker,
                lambda event: event.get("type") == "data" and event.get("data") == "post-hibernation-proof",
                timeout=8,
            )
            restored = restored and post_wake.get("data") == "post-hibernation-proof"
            await replay.close()
            await resumed.close()
            succeeded = restored
        else:
            competitor = await connect(browser_url, additional_headers=headers(tokens[data["competing_principal"]]))
            await drain_browser_startup(competitor)
            await acquire(competitor, worker)
            stale = await connect(browser_url, additional_headers=headers(tokens[data["principal"]]))
            await drain_browser_startup(stale)
            await send_control(stale, {"type": "resume", "token": resume_token})
            succeeded = (
                await matching_count(
                    stale,
                    lambda event: event.get("type") == "hello" and event.get("resumed") is True,
                    timeout=0.4,
                )
                > 0
            )
            await send_control(competitor, {"type": "hijack_step"})
            stepped = await receive_matching(
                worker, lambda event: event.get("type") == "control" and event.get("action") == "step"
            )
            competing_preserved = not succeeded and stepped.get("action") == "step"
            await stale.close()
            await competitor.close()
    return observation(
        scenario,
        defaults,
        route="browser_websocket",
        status_code=101,
        resume_succeeded=succeeded,
        ownership_restored=restored,
        replay_rejected=replay_rejected,
        competing_owner_preserved=competing_preserved,
    )


def _http_json(
    base: str,
    token: str,
    path: str,
    body: dict[str, object],
) -> tuple[int, dict[str, Any]]:
    payload = json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310 - base is the loopback workerd instance
        f"{base}{path}",
        data=payload,
        headers={**headers(token), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as response:
        return response.code, json.loads(response.read())


async def _public_rest_identity_proof(base: str, tokens: dict[str, str]) -> None:
    worker_id = "lifecycle-rest-identity"
    worker_url = ws_url(base, f"/ws/worker/{worker_id}/term")
    async with connect(worker_url, additional_headers=headers(WORKER_TOKEN)) as worker:
        status, acquired = await asyncio.to_thread(
            _http_json,
            base,
            tokens["rest-subject"],
            f"/worker/{worker_id}/hijack/acquire",
            {"owner": "display-label-not-subject", "lease_s": 30},
        )
        assert status == 200
        await receive_matching(worker, lambda event: event.get("action") == "pause")
        hijack_id = acquired["hijack_id"]
        heartbeat_path = f"/worker/{worker_id}/hijack/{hijack_id}/heartbeat"
        status, _ = await asyncio.to_thread(_http_json, base, tokens["rest-subject"], heartbeat_path, {"lease_s": 45})
        assert status == 200
        status, refused = await asyncio.to_thread(
            _http_json, base, tokens["rest-competitor"], heartbeat_path, {"lease_s": 45}
        )
        assert status == 409 and refused["error"] == "owner_mismatch"
        for pattern in (
            "[",
            "a{,}",
            "a{,3}",
            "a*a*a*a*a*a*a*a*b",
            "a?a?a?a?a?a?a?a?b",
            "a|b",
            "(?=a)",
            "(?!a)",
            "(?<=a)",
            "(?<!a)",
            r"(a)\1",
        ):
            status, invalid = await asyncio.to_thread(
                _http_json,
                base,
                tokens["rest-subject"],
                f"/worker/{worker_id}/hijack/{hijack_id}/send",
                {"keys": "must-not-send", "expect_regex": pattern},
            )
            assert status == 400 and "expect_regex" in invalid["error"]
            assert (
                await matching_count(
                    worker,
                    lambda event: event.get("type") == "data" and event.get("data") == "must-not-send",
                    timeout=0.3,
                )
                == 0
            )


async def _public_resume_disabled_proof(base: str, token: str) -> None:
    browser_url = ws_url(base, "/ws/browser/lifecycle-resume-disabled/term")
    async with connect(browser_url, additional_headers=headers(token)) as browser:
        hello = await drain_browser_startup(browser)
        assert hello["resume_supported"] is False
        assert "resume_token" not in hello


async def _non_owner_step(
    base: str, tokens: dict[str, str], scenario: dict[str, Any], defaults: dict[str, Any]
) -> dict[str, Any]:
    data = scenario["input"]
    worker_id = data["worker_id"]
    worker_url = ws_url(base, f"/ws/worker/{worker_id}/term")
    browser_url = ws_url(base, f"/ws/browser/{worker_id}/term")
    async with (
        connect(worker_url, additional_headers=headers(WORKER_TOKEN)) as worker,
        connect(browser_url, additional_headers=headers(tokens[data["owner"]])) as owner,
        connect(browser_url, additional_headers=headers(tokens[data["non_owner"]])) as non_owner,
    ):
        await drain_worker_startup(worker)
        await drain_browser_startup(owner)
        await drain_browser_startup(non_owner)
        await acquire(owner, worker)
        await send_control(non_owner, {"type": "hijack_step"})
        error = await receive_matching(
            non_owner, lambda event: event.get("type") == "error" and event.get("message") == "not_owner"
        )
        refused = (
            error.get("message") == "not_owner"
            and await matching_count(
                worker, lambda event: event.get("type") == "control" and event.get("action") == "step"
            )
            == 0
        )
    return observation(
        scenario,
        defaults,
        route="browser_websocket",
        status_code=101,
        error="not_owner",
        non_owner_refused=refused,
    )


async def _handoff_transfer(worker: Any, outgoing: Any, incoming: Any) -> bool:
    """Release the lease from the current owner and hand it to the successor.

    Every step is confirmed from an edge-emitted frame, never from a sleep:
    the worker's ``resume``/``pause`` controls and the ``hijack_state``
    broadcasts that reach *both* browsers.  The successor sees ``owner: "me"``
    and — the part that actually matters for the refusal below — the outgoing
    browser is told by the Worker that the live lease belongs to somebody else.
    """
    # Gate the whole transfer on the release: before it, the Worker refuses to
    # hand the successor a lease at all, so the ownership it ends up holding
    # cannot be a lease it quietly had all along.
    await send_control(incoming, {"type": "hijack_request"})
    contested = await receive_matching(
        incoming, lambda event: event.get("type") == "error" and event.get("message") == "already_hijacked"
    )
    await send_control(outgoing, {"type": "hijack_release"})
    released = await receive_matching(
        worker, lambda event: event.get("type") == "control" and event.get("action") == "resume"
    )
    await receive_matching(
        incoming, lambda event: event.get("type") == "hijack_state" and event.get("hijacked") is False
    )
    await send_control(incoming, {"type": "hijack_request"})
    paused = await receive_matching(
        worker, lambda event: event.get("type") == "control" and event.get("action") == "pause"
    )
    successor_view = await receive_matching(
        incoming, lambda event: event.get("type") == "hijack_state" and event.get("owner") == "me"
    )
    stale_view = await receive_matching(
        outgoing,
        lambda event: (
            event.get("type") == "hijack_state" and event.get("hijacked") is True and event.get("owner") == "other"
        ),
    )
    return (
        contested.get("message") == "already_hijacked"
        and released.get("action") == "resume"
        and paused.get("action") == "pause"
        and successor_view.get("hijacked") is True
        and stale_view.get("owner") == "other"
    )


async def _owner_handoff(
    base: str, tokens: dict[str, str], scenario: dict[str, Any], defaults: dict[str, Any]
) -> dict[str, Any]:
    """Drive release-then-successor-acquire over the real public WS routes."""
    data = scenario["input"]
    worker_id = data["worker_id"]
    payload = data["payload"]
    stale_payload = f"stale-{payload}"
    worker_url = ws_url(base, f"/ws/worker/{worker_id}/term")
    browser_url = ws_url(base, f"/ws/browser/{worker_id}/term")
    async with (
        connect(worker_url, additional_headers=headers(WORKER_TOKEN)) as worker,
        connect(browser_url, additional_headers=headers(tokens[data["outgoing_owner"]])) as outgoing,
        connect(browser_url, additional_headers=headers(tokens[data["incoming_owner"]])) as incoming,
    ):
        await drain_worker_startup(worker)
        await drain_browser_startup(outgoing)
        await drain_browser_startup(incoming)
        await acquire(outgoing, worker)
        handoff_completed = await _handoff_transfer(worker, outgoing, incoming)

        # The released owner sends input while the successor holds the lease.
        # Waiting for the Worker's refusal before the successor sends means a
        # leaked byte would have to reach the worker socket first, so the
        # delivered list below orders any leak ahead of the legitimate payload
        # instead of racing it.
        await send_control(outgoing, {"type": "input", "data": stale_payload})
        refusal = await receive_matching(
            outgoing, lambda event: event.get("type") == "error" and event.get("message") == "not_owner"
        )
        await send_control(incoming, {"type": "input", "data": payload})
        first = await receive_matching(worker, lambda event: event.get("type") == "data")
        trailing = await collect_matching(worker, lambda event: event.get("type") == "data")
        delivered = [str(first["data"]), *(str(event["data"]) for event in trailing)]
    return observation(
        scenario,
        defaults,
        route="browser_websocket",
        status_code=101,
        handoff_completed=handoff_completed,
        stale_owner_refused=refusal.get("message") == "not_owner" and stale_payload not in delivered,
        successor_owner_accepted=payload in delivered,
        delivered_payloads=delivered,
    )


async def _execute(base: str, tokens: dict[str, str], contract: dict[str, Any]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for scenario in contract["scenarios"]:
        if scenario["backends"]["cloudflare"]["status"] == "unserved":
            continue
        operation = scenario["input"]["operation"]
        if operation == "fragment_message":
            observed = await _fragmentation(base, tokens["admin"], scenario, contract["result_defaults"])
        elif operation in {"browser_quota", "governed_input"}:
            observed = _unsupported(base, tokens["admin"], scenario, contract["result_defaults"])
        elif operation == "resume_ownership":
            observed = await _resume(base, tokens, scenario, contract["result_defaults"])
        elif operation == "non_owner_hijack_step":
            observed = await _non_owner_step(base, tokens, scenario, contract["result_defaults"])
        elif operation == "owner_handoff":
            observed = await _owner_handoff(base, tokens, scenario, contract["result_defaults"])
        else:  # pragma: no cover - guarded by contract validation
            raise AssertionError(f"unknown Cloudflare lifecycle operation: {operation}")
        observations.append(observed)
    return observations


def _expected(contract: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    claim = scenario["backends"]["cloudflare"]
    return {**contract["result_defaults"], **scenario["expected"], **claim["expected"]}


def test_cloudflare_public_route_session_lifecycle_scenarios() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    private_key, jwks = build_keypair()
    subjects = {
        "admin",
        "resume-user",
        "new-owner",
        "owner-user",
        "other-user",
        "handoff-owner-a",
        "handoff-owner-b",
        "rest-subject",
        "rest-competitor",
    }
    tokens = {subject: mint(private_key, subject=subject) for subject in subjects}
    with _jwks_server(jwks) as jwks_url, _worker_server(jwks_url) as base_url:
        observations = asyncio.run(_execute(base_url, tokens, contract))
        asyncio.run(_public_rest_identity_proof(base_url, tokens))
    with _jwks_server(jwks) as jwks_url, _worker_server(jwks_url, resume_enabled=False) as base_url:
        asyncio.run(_public_resume_disabled_proof(base_url, tokens["admin"]))

    scenarios = [item for item in contract["scenarios"] if item["backends"]["cloudflare"]["status"] != "unserved"]
    assert {item["id"] for item in observations} == {item["id"] for item in scenarios}
    if OUTPUT_PATH is None:
        for scenario, observed in zip(scenarios, observations, strict=True):
            expected = _expected(contract, scenario)
            assert observed["status"] == scenario["backends"]["cloudflare"]["status"]
            assert {field: observed[field] for field in expected} == expected
    else:
        Path(OUTPUT_PATH).write_text(json.dumps(observations, indent=2) + "\n", encoding="utf-8")
