//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Pure-helper module split out of hijack.ts: CSS injection, mobile-key data,
 * and toolbar/HTML-string builders that depend only on inputs (no widget state).
 */

export interface MobileKey {
  label: string;
  data: string;
}

export const MOBILE_KEYS: ReadonlyArray<MobileKey> = [
  { label: "ESC", data: "\x1b" },
  { label: "↑", data: "\x1b[A" },
  { label: "↓", data: "\x1b[B" },
  { label: "→", data: "\x1b[C" },
  { label: "←", data: "\x1b[D" },
  { label: "Tab", data: "\t" },
  { label: "^C", data: "\x03" },
  { label: "^D", data: "\x04" },
  { label: "^Z", data: "\x1a" },
];

interface CssInjectionState {
  injected: boolean;
}

const _state: CssInjectionState = { injected: false };

export function _resetHijackCssState(): void {
  _state.injected = false;
}

export function injectHijackCss(baseUrl: string): void {
  if (_state.injected) return;
  _state.injected = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = `${baseUrl}hijack.css`;
  document.head.appendChild(link);
}

export function escapeHijackHtml(value: unknown): string {
  const el = document.createElement("span");
  el.textContent = String(value);
  return el.innerHTML;
}

export interface ToolbarOptions {
  uid: number;
  title: string;
  showAnalysis: boolean;
}

export function buildHijackToolbarHtml(opts: ToolbarOptions): string {
  const p = (id: string) => `h-${opts.uid}-${id}`;
  const analysisSection = opts.showAnalysis
    ? `
      <details class="hijack-analysis" id="${p("analysis")}">
        <summary>Analysis</summary>
        <pre id="${p("analysistext")}"></pre>
      </details>`
    : "";
  return `
      <div class="hijack-toolbar">
        <span class="hijack-title">${escapeHijackHtml(opts.title)}</span>
        <span class="hijack-status">
          <span class="hijack-status-dot" id="${p("dot")}"></span>
          <span id="${p("statustext")}">Connecting…</span>
        </span>
        <div class="hijack-controls">
          <button class="hbtn primary" id="${p("hijack")}" disabled title="Take exclusive control">Hijack</button>
          <button class="hbtn" id="${p("step")}" disabled title="Send one step, then pause">Step</button>
          <button class="hbtn danger" id="${p("release")}" disabled title="Release hijack control">Release</button>
          <button class="hbtn" id="${p("resync")}" disabled title="Request full screen snapshot">⟳ Resync</button>
          <button class="hbtn" id="${p("analyze")}" disabled title="AI-readable screen description">Analyze</button>
          <button class="hbtn" id="${p("kbdtoggle")}" title="Toggle mobile key toolbar">⌨</button>
        </div>
        <span class="hijack-prompt" id="${p("prompt")}" title="Current prompt ID"></span>
      </div>
      <div class="hijack-terminal" id="${p("terminal")}"></div>
      <div class="hijack-input-row" id="${p("inputrow")}">
        <input class="hijack-input-field" id="${p("inputfield")}"
          placeholder="Send keys… (Enter to send, e.g. \\r for Return)"
          autocomplete="off" spellcheck="false">
        <button class="hijack-input-send" id="${p("inputsend")}">Send</button>
      </div>
      <div class="mobile-keys" id="${p("mobilekeys")}"></div>
      ${analysisSection}
    `;
}
