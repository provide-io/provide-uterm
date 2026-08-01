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
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import websockets
from cf_jwt_harness import AUDIENCE, ISSUER, build_keypair, mint

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder, DataChunk, encode_control_frame

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
CONTRACT_PATH = Path(
    os.environ.get(
        "SESSION_LIFECYCLE_SCENARIO_CONTRACT",
        REPO_ROOT / "spec/session_lifecycle_security_scenarios.json",
    )
)
OUTPUT_PATH = os.environ.get("SESSION_LIFECYCLE_SCENARIO_OUTPUT")
WORKER_TOKEN = "lifecycle-worker-token-padded-beyond-32-characters"


def _observation(scenario: dict[str, Any], defaults: dict[str, Any], **values: Any) -> dict[str, Any]:
    return {"id": scenario["id"], "status": scenario["backends"]["cloudflare"]["status"], **defaults, **values}


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
    (PACKAGE_ROOT / ".venv-workers/.synced").touch()
    (PACKAGE_ROOT / "python_modules/.synced").touch()


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
def _worker_server(jwks_url: str) -> Iterator[str]:
    _prepare_worker_vendor_tree()
    pywrangler = shutil.which("pywrangler")
    assert pywrangler is not None, "pywrangler is required for the native Cloudflare lifecycle adapter"
    node_bin = _node22_bin()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = {**os.environ, "PATH": f"{node_bin}{os.pathsep}{os.environ.get('PATH', '')}"}
    command = [
        pywrangler,
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
        f"WORKER_BEARER_TOKEN:{WORKER_TOKEN}",
    ]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log:
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


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://", 1) + path


def _decode(raw: str | bytes) -> list[dict[str, Any]]:
    if not isinstance(raw, str):
        return []
    events: list[dict[str, Any]] = []
    for event in ControlFrameDecoder().feed(raw):
        if isinstance(event, ControlChunk):
            events.append(event.control)
        elif isinstance(event, DataChunk):
            events.append({"type": "data", "data": event.data})
    return events


async def _receive_matching(
    websocket: Any,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 4,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(websocket.recv(), timeout=max(0.01, deadline - asyncio.get_running_loop().time()))
        for event in _decode(raw):
            if predicate(event):
                return event
    raise TimeoutError("matching WebSocket frame was not observed")


async def _matching_count(
    websocket: Any,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 0.2,
) -> int:
    count = 0
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=deadline - asyncio.get_running_loop().time())
        except TimeoutError:
            break
        count += sum(predicate(event) for event in _decode(raw))
    return count


async def _send_control(websocket: Any, frame: dict[str, Any]) -> None:
    await websocket.send(encode_control_frame(frame))


async def _drain_worker_startup(worker: Any) -> None:
    # Registration is completed eagerly before the 101 response.  The CF
    # protocol doesn't emit an unsolicited worker hello/snapshot request.
    assert worker.state.name == "OPEN"


async def _drain_browser_startup(browser: Any) -> dict[str, Any]:
    return await _receive_matching(browser, lambda event: event.get("type") == "hello")


async def _acquire(browser: Any, worker: Any) -> None:
    await _send_control(browser, {"type": "hijack_request"})
    await _receive_matching(worker, lambda event: event.get("type") == "control" and event.get("action") == "pause")
    await _receive_matching(browser, lambda event: event.get("type") == "hijack_state" and event.get("owner") == "me")


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
    before = await _matching_count(receiver, predicate)
    finish.set()
    await asyncio.wait_for(task, timeout=4)
    await _receive_matching(receiver, predicate)
    return before, 1 + await _matching_count(receiver, predicate)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _connect(url: str, *, additional_headers: dict[str, str]) -> Any:
    """Open a socket with bounded cleanup for workerd hibernation sockets."""
    return websockets.connect(url, additional_headers=additional_headers, close_timeout=0.2)


async def _fragmentation(base: str, token: str, scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    data = scenario["input"]
    worker_id = data["worker_id"]
    payload = data["payload"]
    count = data["fragment_count"]
    worker_url = _ws_url(base, f"/ws/worker/{worker_id}/term")
    browser_url = _ws_url(base, f"/ws/browser/{worker_id}/term")

    def is_payload(event: dict[str, Any]) -> bool:
        return event.get("type") == "data" and event.get("data") == payload

    async with (
        _connect(worker_url, additional_headers=_headers(WORKER_TOKEN)) as worker,
        _connect(browser_url, additional_headers=_headers(token)) as browser,
    ):
        await _drain_worker_startup(worker)
        await _drain_browser_startup(browser)
        if data["transport"] == "browser":
            await _acquire(browser, worker)
            wire_payload = encode_control_frame({"type": "input", "data": payload})
            before, after = await _fragment_counts(browser, wire_payload, count, worker, is_payload)
            await _send_control(browser, {"type": "input", "data": "X" * data["oversized_bytes"]})
            error = await _receive_matching(browser, lambda event: event.get("type") == "error")
            oversized_refused = "too large" in str(error.get("message"))
            route = "browser_websocket"
        else:
            before, after = await _fragment_counts(worker, payload, count, browser, is_payload)
            await worker.send("X" * data["oversized_bytes"])
            error = await _receive_matching(worker, lambda event: event.get("type") == "error")
            oversized_refused = "too large" in str(error.get("message"))
            route = "worker_websocket"
    return _observation(
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
        f"{base}/api/lifecycle/{suffix}", headers=_headers(token)
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
    return _observation(scenario, defaults, **values)


async def _resume(
    base: str, tokens: dict[str, str], scenario: dict[str, Any], defaults: dict[str, Any]
) -> dict[str, Any]:
    data = scenario["input"]
    worker_id = data["worker_id"]
    worker_url = _ws_url(base, f"/ws/worker/{worker_id}/term")
    browser_url = _ws_url(base, f"/ws/browser/{worker_id}/term")
    restored = replay_rejected = competing_preserved = False
    async with _connect(worker_url, additional_headers=_headers(WORKER_TOKEN)) as worker:
        await _drain_worker_startup(worker)
        original = await _connect(browser_url, additional_headers=_headers(tokens[data["principal"]]))
        hello = await _drain_browser_startup(original)
        resume_token = hello["resume_token"]
        await _acquire(original, worker)
        await original.close()
        await _receive_matching(
            worker, lambda event: event.get("type") == "control" and event.get("action") == "resume"
        )
        if data["case"] == "current_owner":
            resumed = await _connect(browser_url, additional_headers=_headers(tokens[data["principal"]]))
            await _drain_browser_startup(resumed)
            await _send_control(resumed, {"type": "resume", "token": resume_token})
            resumed_hello = await _receive_matching(
                resumed, lambda event: event.get("type") == "hello" and event.get("resumed") is True
            )
            await _receive_matching(
                worker, lambda event: event.get("type") == "control" and event.get("action") == "pause"
            )
            await _send_control(resumed, {"type": "input", "data": "resume-proof"})
            restored = bool(resumed_hello["resumed"]) and bool(
                await _receive_matching(
                    worker, lambda event: event.get("type") == "data" and event.get("data") == "resume-proof"
                )
            )
            replay = await _connect(browser_url, additional_headers=_headers(tokens[data["principal"]]))
            await _drain_browser_startup(replay)
            await _send_control(replay, {"type": "resume", "token": resume_token})
            replay_rejected = (
                await _matching_count(
                    replay,
                    lambda event: event.get("type") == "hello" and event.get("resumed") is True,
                    timeout=0.4,
                )
                == 0
            )
            await replay.close()
            await resumed.close()
            succeeded = restored
        else:
            competitor = await _connect(browser_url, additional_headers=_headers(tokens[data["competing_principal"]]))
            await _drain_browser_startup(competitor)
            await _acquire(competitor, worker)
            stale = await _connect(browser_url, additional_headers=_headers(tokens[data["principal"]]))
            await _drain_browser_startup(stale)
            await _send_control(stale, {"type": "resume", "token": resume_token})
            succeeded = (
                await _matching_count(
                    stale,
                    lambda event: event.get("type") == "hello" and event.get("resumed") is True,
                    timeout=0.4,
                )
                > 0
            )
            await _send_control(competitor, {"type": "hijack_step"})
            stepped = await _receive_matching(
                worker, lambda event: event.get("type") == "control" and event.get("action") == "step"
            )
            competing_preserved = not succeeded and stepped.get("action") == "step"
            await stale.close()
            await competitor.close()
    return _observation(
        scenario,
        defaults,
        route="browser_websocket",
        status_code=101,
        resume_succeeded=succeeded,
        ownership_restored=restored,
        replay_rejected=replay_rejected,
        competing_owner_preserved=competing_preserved,
    )


async def _non_owner_step(
    base: str, tokens: dict[str, str], scenario: dict[str, Any], defaults: dict[str, Any]
) -> dict[str, Any]:
    data = scenario["input"]
    worker_id = data["worker_id"]
    worker_url = _ws_url(base, f"/ws/worker/{worker_id}/term")
    browser_url = _ws_url(base, f"/ws/browser/{worker_id}/term")
    async with (
        _connect(worker_url, additional_headers=_headers(WORKER_TOKEN)) as worker,
        _connect(browser_url, additional_headers=_headers(tokens[data["owner"]])) as owner,
        _connect(browser_url, additional_headers=_headers(tokens[data["non_owner"]])) as non_owner,
    ):
        await _drain_worker_startup(worker)
        await _drain_browser_startup(owner)
        await _drain_browser_startup(non_owner)
        await _acquire(owner, worker)
        await _send_control(non_owner, {"type": "hijack_step"})
        error = await _receive_matching(
            non_owner, lambda event: event.get("type") == "error" and event.get("message") == "not_owner"
        )
        refused = (
            error.get("message") == "not_owner"
            and await _matching_count(
                worker, lambda event: event.get("type") == "control" and event.get("action") == "step"
            )
            == 0
        )
    return _observation(
        scenario,
        defaults,
        route="browser_websocket",
        status_code=101,
        error="not_owner",
        non_owner_refused=refused,
    )


async def _execute(base: str, tokens: dict[str, str], contract: dict[str, Any]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for scenario in contract["scenarios"]:
        if scenario["backends"]["cloudflare"]["status"] == "unserved":
            continue
        operation = scenario["input"]["operation"]
        if operation == "fragment_message":
            observation = await _fragmentation(base, tokens["admin"], scenario, contract["result_defaults"])
        elif operation in {"browser_quota", "governed_input"}:
            observation = _unsupported(base, tokens["admin"], scenario, contract["result_defaults"])
        elif operation == "resume_ownership":
            observation = await _resume(base, tokens, scenario, contract["result_defaults"])
        elif operation == "non_owner_hijack_step":
            observation = await _non_owner_step(base, tokens, scenario, contract["result_defaults"])
        else:  # pragma: no cover - guarded by contract validation
            raise AssertionError(f"unknown Cloudflare lifecycle operation: {operation}")
        observations.append(observation)
    return observations


def _expected(contract: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    claim = scenario["backends"]["cloudflare"]
    return {**contract["result_defaults"], **scenario["expected"], **claim["expected"]}


def test_cloudflare_public_route_session_lifecycle_scenarios() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    private_key, jwks = build_keypair()
    subjects = {"admin", "resume-user", "new-owner", "owner-user", "other-user"}
    tokens = {subject: mint(private_key, subject=subject) for subject in subjects}
    with _jwks_server(jwks) as jwks_url, _worker_server(jwks_url) as base_url:
        observations = asyncio.run(_execute(base_url, tokens, contract))

    scenarios = [item for item in contract["scenarios"] if item["backends"]["cloudflare"]["status"] != "unserved"]
    assert {item["id"] for item in observations} == {item["id"] for item in scenarios}
    if OUTPUT_PATH is None:
        for scenario, observation in zip(scenarios, observations, strict=True):
            expected = _expected(contract, scenario)
            assert observation["status"] == scenario["backends"]["cloudflare"]["status"]
            assert {field: observation[field] for field in expected} == expected
    else:
        Path(OUTPUT_PATH).write_text(json.dumps(observations, indent=2) + "\n", encoding="utf-8")
