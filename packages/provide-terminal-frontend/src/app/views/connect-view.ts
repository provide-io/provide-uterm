//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { quickConnect } from "../api.js";
import type { AppBootstrap } from "../types.js";
import { renderAppHeader } from "./app-header.js";

function escapeHtml(value: string): string {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function updateFieldVisibility(form: HTMLFormElement): void {
  const type = (form.querySelector("#connect-type") as HTMLSelectElement).value;
  const needsHost = type === "ssh" || type === "telnet";
  for (const el of form.querySelectorAll<HTMLElement>(".field-host")) {
    el.style.display = needsHost ? "" : "none";
  }
  for (const el of form.querySelectorAll<HTMLElement>(".field-ssh")) {
    el.style.display = type === "ssh" ? "" : "none";
  }
  const portEl = form.querySelector<HTMLInputElement>("#connect-port");
  if (portEl && !portEl.dataset.userEdited) {
    portEl.value = type === "telnet" ? "23" : "22";
  }
}

async function handleSubmit(form: HTMLFormElement, errorEl: HTMLElement, submitBtn: HTMLButtonElement): Promise<void> {
  errorEl.textContent = "";
  const type = (form.querySelector("#connect-type") as HTMLSelectElement).value;
  const host = (form.querySelector("#connect-host") as HTMLInputElement).value.trim();
  if ((type === "ssh" || type === "telnet") && !host) {
    errorEl.textContent = `Host is required for ${type.toUpperCase()} connections.`;
    return;
  }
  submitBtn.disabled = true;
  submitBtn.textContent = "Connecting\u2026";
  const payload: Record<string, unknown> = { connector_type: type };
  const name = (form.querySelector("#connect-name") as HTMLInputElement).value.trim();
  if (name) payload.display_name = name;
  const mode = (form.querySelector("#connect-mode") as HTMLSelectElement).value;
  if (mode) payload.input_mode = mode;
  const tagsRaw = (form.querySelector("#connect-tags") as HTMLInputElement).value.trim();
  if (tagsRaw) {
    payload.tags = tagsRaw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  if (type === "ssh" || type === "telnet") {
    payload.host = host;
    payload.port =
      parseInt((form.querySelector("#connect-port") as HTMLInputElement).value, 10) || (type === "telnet" ? 23 : 22);
  }
  if (type === "ssh") {
    const user = (form.querySelector("#connect-user") as HTMLInputElement).value.trim();
    const pass = (form.querySelector("#connect-pass") as HTMLInputElement).value;
    if (user) payload.username = user;
    if (pass) payload.password = pass;
  }
  try {
    const result = await quickConnect(payload as unknown as Parameters<typeof quickConnect>[0]);
    window.location.href = result.url;
  } catch (err) {
    errorEl.textContent = err instanceof Error ? err.message : "Connection failed.";
    submitBtn.disabled = false;
    submitBtn.textContent = "Connect";
  }
}

export function renderConnect(root: HTMLElement, bootstrap: AppBootstrap): void {
  const safeAppPath = escapeHtml(bootstrap.app_path);
  root.innerHTML = `
    <div class="page">
      ${renderAppHeader(bootstrap, "connect")}
      <div class="card" style="max-width:480px;margin:2rem auto">
        <div class="small" style="margin-bottom:.75rem">
          <a href="${safeAppPath}/">&#8592; Dashboard</a>
        </div>
        <h2 style="margin-bottom:1.25rem">Quick Connect</h2>
        <form id="connect-form">
          <div class="field">
            <label for="connect-type">Connection type</label>
            <select id="connect-type">
              <option value="ssh">SSH</option>
              <option value="telnet">Telnet</option>
              <option value="shell">Local Shell</option>
            </select>
          </div>
          <div class="field">
            <label for="connect-name">Display name (optional)</label>
            <input id="connect-name" type="text" placeholder="My session">
          </div>
          <div class="field field-host">
            <label for="connect-host">Host</label>
            <input id="connect-host" type="text" placeholder="hostname or IP">
          </div>
          <div class="field field-host">
            <label for="connect-port">Port</label>
            <input id="connect-port" type="number" value="22" min="1" max="65535">
          </div>
          <div class="field field-ssh">
            <label for="connect-user">Username</label>
            <input id="connect-user" type="text" placeholder="username">
          </div>
          <div class="field field-ssh">
            <label for="connect-pass">Password</label>
            <input id="connect-pass" type="password" placeholder="password">
          </div>
          <div class="field">
            <label for="connect-mode">Input mode</label>
            <select id="connect-mode">
              <option value="open">Open (shared input)</option>
              <option value="hijack">Exclusive (hijack only)</option>
            </select>
          </div>
          <div class="field">
            <label for="connect-tags">Tags (optional, comma-separated)</label>
            <input id="connect-tags" type="text" placeholder="game, prod, demo">
          </div>
          <div id="connect-error" class="field-error"></div>
          <button id="connect-submit" class="btn primary" type="submit" style="width:100%">Connect</button>
        </form>
      </div>
    </div>
  `;
  const form = root.querySelector<HTMLFormElement>("#connect-form");
  const errorEl = root.querySelector<HTMLElement>("#connect-error");
  const submitBtn = root.querySelector<HTMLButtonElement>("#connect-submit");
  const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type");
  const portEl = root.querySelector<HTMLInputElement>("#connect-port");
  if (!form || !errorEl || !submitBtn || !typeSelect || !portEl) return;
  updateFieldVisibility(form);
  typeSelect.addEventListener("change", () => updateFieldVisibility(form));
  portEl.addEventListener("input", function (this: HTMLInputElement) {
    this.dataset.userEdited = "1";
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    void handleSubmit(form, errorEl, submitBtn);
  });
}
