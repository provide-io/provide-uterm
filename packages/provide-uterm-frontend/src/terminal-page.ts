//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

function sanitizeWorkerId(value: string | null): string {
  if (!value) return "demo";
  return /^[A-Za-z0-9_-]{1,64}$/.test(value) ? value : "demo";
}

function resolveWsPath(): string {
  const params = new URLSearchParams(window.location.search);
  const workerId = sanitizeWorkerId(params.get("worker_id"));
  const roleParam = params.get("role");
  const role = roleParam === "browser" ? "browser" : "raw";
  return `/ws/${role}/${workerId}/term`;
}

function initTerminalPage(): void {
  const container = document.getElementById("app");
  if (!(container instanceof HTMLElement)) {
    throw new Error("Missing #app container");
  }
  const widget = document.createElement("uterm-terminal") as any;
  widget.config = {
    wsUrl: resolveWsPath(),
    title: "Provide Terminal Cloudflare",
  };
  container.appendChild(widget);
}

initTerminalPage();
