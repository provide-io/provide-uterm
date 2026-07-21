#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Build/run the uterm-test-vnc lab image and prove RFB + browser navigation.

Live path (not mocked):
  1. ``docker build`` the committed Dockerfile under ``docker/vnc-lab/``
  2. ``docker run`` with a published RFB port
  3. Real RFB protocol version + security-type handshake against 127.0.0.1
  4. Collect in-container navigation evidence (browser-nav.log + process list)
  5. Tear down the container

Usage (repo root)::

    uv run python scripts/prove_vnc_lab.py
    uv run python scripts/prove_vnc_lab.py --evidence-dir /path --runs 2
    uv run python scripts/prove_vnc_lab.py --skip-build

Exit 0 only when every run succeeds. Artifacts land under ``--evidence-dir``
(or ``$EVIDENCE_DIR`` / ``$SCRATCH`` / a temp dir).
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

IMAGE_NAME = "uterm-test-vnc"
CONTAINER_NAME = "uterm-test-vnc-prove"
DEMO_URL = "https://example.com"
RFB_PLAIN_PORT = 5900
RFB_SSL_PORT = 5901
DEFAULT_SHM = "256m"

# RFB protocol version strings (12 bytes including newline)
_RFB_VERSION_38 = b"RFB 003.008\n"
_RFB_VERSION_37 = b"RFB 003.007\n"
_RFB_VERSION_33 = b"RFB 003.003\n"
# Back-compat alias used by tests/callers
_RFB_VERSION = _RFB_VERSION_38


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _run(
    cmd: list[str],
    *,
    timeout: float,
    check: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        cwd=str(cwd) if cwd else None,
    )


def build_image(*, root: Path, log_path: Path) -> None:
    """Build the committed vnc-lab Dockerfile into IMAGE_NAME."""
    context = root / "docker" / "vnc-lab"
    dockerfile = context / "Dockerfile"
    if not dockerfile.is_file():
        raise FileNotFoundError(f"missing Dockerfile: {dockerfile}")

    cmd = [
        "docker",
        "build",
        "-t",
        IMAGE_NAME,
        "-f",
        str(dockerfile),
        str(context),
    ]
    result = _run(cmd, timeout=600, check=False)
    log_path.write_text(
        f"$ {' '.join(cmd)}\n"
        f"exit={result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}\n",
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker build failed (exit {result.returncode}); see {log_path}")


def _remove_container(name: str) -> None:
    _run(["docker", "rm", "-f", name], timeout=60, check=False)


def _mapped_port(name: str, container_port: int) -> int:
    port_result = _run(
        ["docker", "port", name, str(container_port)],
        timeout=15,
        check=False,
    )
    if port_result.returncode != 0 or not port_result.stdout.strip():
        raise RuntimeError(f"docker port {container_port} failed: {port_result.stderr.strip()[:300]}")
    # e.g. "0.0.0.0:32768" or ":::32768"
    port_line = port_result.stdout.strip().splitlines()[-1]
    return int(port_line.rsplit(":", 1)[-1])


def start_container(*, name: str, demo_url: str) -> tuple[int, int]:
    """Start the lab container; return (plain_host_port, ssl_host_port)."""
    _remove_container(name)
    result = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--shm-size",
            DEFAULT_SHM,
            "-p",
            f"0:{RFB_PLAIN_PORT}",
            "-p",
            f"0:{RFB_SSL_PORT}",
            "-e",
            f"DEMO_URL={demo_url}",
            IMAGE_NAME,
        ],
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker run failed: {result.stderr.strip()[:500]}")

    return _mapped_port(name, RFB_PLAIN_PORT), _mapped_port(name, RFB_SSL_PORT)


def rfb_handshake(
    host: str,
    port: int,
    *,
    timeout: float = 5.0,
    client_version: bytes | None = None,
    sock: socket.socket | ssl.SSLSocket | None = None,
    transport: str = "plain",
) -> str:
    """Complete a real RFB version + security-type exchange; return summary.

    Speaks enough of RFB 3.3/3.8 to prove the peer is a VNC server, not merely
    that TCP is open. Stops after SecurityResult (or after the RFB 3.3 security
    type is accepted).

    If *sock* is provided it is used as the already-connected transport (e.g.
    TLS-wrapped). The caller retains ownership and must close it.
    """
    owns_sock = sock is None
    if sock is None:
        sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        server_version = sock.recv(12)
        if len(server_version) != 12 or not server_version.startswith(b"RFB "):
            raise RuntimeError(f"invalid RFB ProtocolVersion from server: {server_version!r}")
        # Prefer an explicit client version (compat matrix); else echo server;
        # fall back to 3.8.
        if client_version is not None:
            chosen = client_version
        elif server_version[0:4] == b"RFB " and server_version.endswith(b"\n"):
            chosen = server_version
        else:
            chosen = _RFB_VERSION_38
        sock.sendall(chosen)

        lines = [
            f"rfb_connect=ok host={host} port={port} transport={transport}",
            f"server_protocol_version={server_version!r}",
            f"client_protocol_version={chosen!r}",
        ]

        # Parse "RFB 003.008\n" → minor 8. RFB 3.3 uses a 4-byte security type;
        # 3.7+ uses U8 count + type list. After we pick a client version, the
        # subsequent security negotiation follows that client's rules.
        try:
            client_minor = int(chosen[8:11])
        except ValueError:
            client_minor = 8

        if client_minor <= 3:
            sec_raw = _recv_exact(sock, 4)
            sec_type = int.from_bytes(sec_raw, "big")
            lines.append(f"security_type_rfb33={sec_type}")
            if sec_type == 0:
                raise RuntimeError("RFB server rejected connection (sec type 0)")
            # None (1) is the lab default; VNC Auth (2) is also acceptable if set.
            if sec_type not in (1, 2):
                raise RuntimeError(f"unexpected RFB 3.3 security type: {sec_type}")
            lines.append("rfb_handshake=ok")
            return "\n".join(lines) + "\n"

        # RFB 3.7 / 3.8
        n_raw = _recv_exact(sock, 1)
        n_types = n_raw[0]
        if n_types == 0:
            # Failure path: 4-byte reason length + reason string
            reason_len = int.from_bytes(_recv_exact(sock, 4), "big")
            reason = sock.recv(min(reason_len, 1024)) if reason_len else b""
            raise RuntimeError(f"RFB security handshake failed: {reason!r}")
        types = _recv_exact(sock, n_types)
        lines.append(f"security_types={list(types)}")
        if 1 not in types:
            raise RuntimeError(f"lab VNC must offer Security None (1); got {list(types)}")
        sock.sendall(bytes([1]))  # select None
        # SecurityResult: 4 bytes, 0 = OK (RFB 3.8 always; some servers omit)
        sock.settimeout(2.0)
        try:
            result = sock.recv(4)
        except TimeoutError:
            result = b""
        lines.append(f"security_result={result!r}")
        if len(result) == 4 and result != b"\x00\x00\x00\x00":
            raise RuntimeError(f"RFB SecurityResult not OK: {result!r}")
        lines.append("rfb_handshake=ok")
        return "\n".join(lines) + "\n"
    finally:
        if owns_sock and sock is not None:
            sock.close()


def _recv_exact(sock: socket.socket | ssl.SSLSocket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError(f"RFB short read: want {n}, got {len(buf)}")
        buf.extend(chunk)
    return bytes(buf)


def rfb_handshake_tls(host: str, port: int, *, timeout: float = 5.0) -> str:
    """TLS-first encrypted path (x11vnc -ssl): wrap TCP, then RFB handshake."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Prefer modern TLS but allow down to 1.2 for older VNC stacks.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        tls = ctx.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise
    try:
        tls_ver = tls.version() or "unknown"
        summary = rfb_handshake(
            host,
            port,
            timeout=timeout,
            sock=tls,
            transport=f"tls({tls_ver})",
        )
        return f"tls_version={tls_ver}\n{summary}"
    finally:
        tls.close()


def wait_rfb(
    host: str,
    port: int,
    *,
    retries: int = 60,
    delay: float = 0.5,
    tls: bool = False,
    client_version: bytes | None = None,
) -> str:
    last_err: Exception | None = None
    for _attempt in range(retries):
        try:
            if tls:
                return rfb_handshake_tls(host, port)
            return rfb_handshake(host, port, client_version=client_version)
        except (OSError, RuntimeError, ssl.SSLError) as exc:
            last_err = exc
            time.sleep(delay)
    kind = "TLS RFB" if tls else "plain RFB"
    raise RuntimeError(f"{kind} handshake failed after {retries} tries: {last_err}")


def collect_navigation_evidence(name: str, demo_url: str) -> str:
    """Read browser-nav.log and process list; require demo_url evidence."""
    chunks: list[str] = []

    nav = _run(
        ["docker", "exec", name, "cat", "/var/log/vnc-lab/browser-nav.log"],
        timeout=30,
        check=False,
    )
    chunks.append("--- browser-nav.log ---")
    chunks.append(nav.stdout if nav.returncode == 0 else f"(exit {nav.returncode}) {nav.stderr}")

    # Process list (busybox/debian ps)
    ps = _run(
        ["docker", "exec", name, "ps", "auxww"],
        timeout=30,
        check=False,
    )
    chunks.append("--- ps auxww ---")
    chunks.append(ps.stdout if ps.returncode == 0 else f"(exit {ps.returncode}) {ps.stderr}")

    ready = _run(
        ["docker", "exec", name, "cat", "/var/log/vnc-lab/vnc-ready"],
        timeout=15,
        check=False,
    )
    chunks.append("--- vnc-ready ---")
    ready_text = ready.stdout.strip() if ready.returncode == 0 else "(missing)"
    chunks.append(ready_text)

    text = "\n".join(chunks) + "\n"
    if demo_url not in text:
        raise RuntimeError(f"navigation evidence missing demo URL {demo_url!r} in container logs/ps")
    if "browser_nav_url=" not in text:
        raise RuntimeError("browser-nav.log missing browser_nav_url= marker")
    # Dual-mode readiness markers from the entrypoint.
    if "rfb_plain_port=" not in ready_text or "rfb_ssl_port=" not in ready_text:
        raise RuntimeError("vnc-ready missing dual-port markers (plain + ssl)")
    if "vencrypt" not in ready_text.lower() and "ssl" not in ready_text.lower():
        raise RuntimeError("vnc-ready missing encrypted mode markers")
    # Require a chromium-ish process still related to the URL or binary.
    lower = text.lower()
    if "chromium" not in lower and "chrome" not in lower:
        raise RuntimeError("no chromium/chrome process found in navigation evidence")
    return text


def prove_once(
    *,
    root: Path,
    evidence_dir: Path,
    run_index: int,
    skip_build: bool,
    demo_url: str,
    container_name: str,
) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    suffix = "" if run_index == 1 else f"-{run_index}"
    connect_log = evidence_dir / f"vnc-connect{suffix}.log"
    browser_log = evidence_dir / f"browser-nav{suffix}.log"
    # First run also owns the build log name required by the plan.
    if run_index == 1 and not skip_build:
        build_image(root=root, log_path=evidence_dir / "vnc-image-build.log")
    elif run_index == 1 and skip_build:
        # Still record that build was skipped for the verifier.
        (evidence_dir / "vnc-image-build.log").write_text(
            "skip-build=1\n"
            f"image={IMAGE_NAME}\n"
            "note=reuse existing local image; Dockerfile path docker/vnc-lab/Dockerfile\n",
            encoding="utf-8",
        )

    try:
        plain_port, ssl_port = start_container(name=container_name, demo_url=demo_url)

        # --- Unencrypted path (classic RFB) ---
        plain = wait_rfb("127.0.0.1", plain_port, tls=False)
        if "rfb_handshake=ok" not in plain:
            raise RuntimeError("plain RFB handshake missing rfb_handshake=ok")

        # Version-compat matrix on the plain port: client 3.3, 3.7, 3.8.
        compat_chunks: list[str] = ["=== plain RFB (unencrypted) ===", plain.rstrip(), ""]
        for label, ver in (
            ("client_3.8", _RFB_VERSION_38),
            ("client_3.7", _RFB_VERSION_37),
            ("client_3.3", _RFB_VERSION_33),
        ):
            # Fresh TCP each time — one handshake per connection.
            chunk = wait_rfb(
                "127.0.0.1",
                plain_port,
                retries=10,
                delay=0.3,
                tls=False,
                client_version=ver,
            )
            if "rfb_handshake=ok" not in chunk:
                raise RuntimeError(f"plain RFB {label} failed")
            compat_chunks.append(f"=== plain {label} ===")
            compat_chunks.append(chunk.rstrip())
            compat_chunks.append("")

        # --- Encrypted path (TLS-first x11vnc -ssl; VeNCrypt/ANONTLS also enabled) ---
        encrypted = wait_rfb("127.0.0.1", ssl_port, tls=True)
        if "rfb_handshake=ok" not in encrypted:
            raise RuntimeError("TLS RFB handshake missing rfb_handshake=ok")
        if "tls_version=" not in encrypted:
            raise RuntimeError("TLS RFB handshake missing tls_version=")
        compat_chunks.append("=== tls RFB (encrypted) ===")
        compat_chunks.append(encrypted.rstrip())
        compat_chunks.append("")
        compat_chunks.append(f"plain_host_port={plain_port}")
        compat_chunks.append(f"ssl_host_port={ssl_port}")
        compat_chunks.append("modes_proven=plain,tls")
        compat_chunks.append("rfb_versions_proven=3.3,3.7,3.8")

        connect_log.write_text("\n".join(compat_chunks) + "\n", encoding="utf-8")
        # Dedicated SSL log for dual-run evidence naming.
        ssl_log = evidence_dir / f"vnc-connect-ssl{suffix}.log"
        ssl_log.write_text(encrypted, encoding="utf-8")

        # Allow Chromium a moment to start after RFB is already up.
        time.sleep(2.0)
        nav = collect_navigation_evidence(container_name, demo_url)
        browser_log.write_text(nav, encoding="utf-8")
    finally:
        _remove_container(container_name)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="Directory for logs (default: $EVIDENCE_DIR / $SCRATCH / temp)",
    )
    p.add_argument(
        "--runs",
        type=int,
        default=1,
        help="How many successive prove cycles (default 1; plan wants 2)",
    )
    p.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip docker build (reuse existing uterm-test-vnc image)",
    )
    p.add_argument(
        "--demo-url",
        default=DEMO_URL,
        help=f"URL Chromium must open (default {DEMO_URL})",
    )
    p.add_argument(
        "--container-name",
        default=CONTAINER_NAME,
        help=f"Docker container name (default {CONTAINER_NAME})",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = _repo_root()

    if not _docker_available():
        evidence = _resolve_evidence_dir(args.evidence_dir)
        evidence.mkdir(parents=True, exist_ok=True)
        msg = "docker not found on PATH\n"
        (evidence / "docker-unavailable.log").write_text(msg, encoding="utf-8")
        print(msg, file=sys.stderr)
        return 2

    evidence = _resolve_evidence_dir(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)

    for i in range(1, args.runs + 1):
        print(f"prove_vnc_lab: run {i}/{args.runs} evidence={evidence}", flush=True)
        prove_once(
            root=root,
            evidence_dir=evidence,
            run_index=i,
            skip_build=args.skip_build or i > 1,
            demo_url=args.demo_url,
            container_name=args.container_name,
        )
        print(f"prove_vnc_lab: run {i} OK", flush=True)

    print(f"prove_vnc_lab: all {args.runs} run(s) succeeded; evidence in {evidence}")
    return 0


def _resolve_evidence_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    for key in ("EVIDENCE_DIR", "SCRATCH"):
        val = os.environ.get(key)
        if val:
            return Path(val)
    return Path(os.environ.get("TMPDIR", "/tmp")) / "uterm-vnc-lab-prove"


if __name__ == "__main__":
    raise SystemExit(main())
