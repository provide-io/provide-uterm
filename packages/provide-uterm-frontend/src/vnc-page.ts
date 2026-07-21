//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/**
 * First-party VNC console page: noVNC RFB client over uterm binary WS relay.
 *
 * Query params:
 *   worker_id, hijack_id, target_id (required for connect)
 *   view_only=1 (optional)
 *   token / access_token — optional Bearer for environments that put JWT in query
 *
 * The RFB client is loaded dynamically so unit tests of pure helpers do not
 * pull in noVNC's CJS graph under vitest.
 */

import {
  buildVncWsUrl,
  readVncPageParams,
  statusFromCloseCode,
  type VncPageParams,
  type VncStatusState,
} from "./vnc-url.js";

export {
  buildVncWsUrl,
  readVncPageParams,
  sanitizeId,
  statusFromCloseCode,
  type VncPageParams,
  type VncStatusInfo,
  type VncStatusState,
} from "./vnc-url.js";

/** Import path of the real RFB client (asserted by structural tests). */
export const NOVNC_RFB_MODULE = "@novnc/novnc";

type RfbInstance = {
  viewOnly: boolean;
  scaleViewport: boolean;
  resizeSession: boolean;
  background: string;
  addEventListener: (type: string, listener: (ev: Event) => void) => void;
  removeEventListener: (type: string, listener: (ev: Event) => void) => void;
  disconnect: () => void;
};

export type RfbConstructor = new (
  target: HTMLElement,
  urlOrChannel: string | WebSocket,
  options?: Record<string, unknown>,
) => RfbInstance;

/** Unwrap CJS/ESM default-export nests until we have a constructor. */
export function resolveRfbConstructor(mod: unknown): RfbConstructor {
  let cur: unknown = mod;
  // Vite/CJS interop can nest: module → { default: RFB } → { default: RFB }.
  for (let i = 0; i < 4; i++) {
    if (typeof cur === "function") {
      return cur as RfbConstructor;
    }
    if (cur && typeof cur === "object" && "default" in (cur as object)) {
      cur = (cur as { default: unknown }).default;
      continue;
    }
    break;
  }
  throw new Error(`noVNC RFB export is not a constructor (got ${typeof cur})`);
}

async function loadRfbClass(): Promise<RfbConstructor> {
  // Real noVNC client — not a local RFB reimplementation.
  // Dynamic import keeps the heavy graph out of unit-test imports of pure
  // helpers (see vnc-url.ts); Vite still bundles this into the page chunk.
  // Package export is ESM core/rfb.js as of @novnc/novnc@1.7.
  const mod = await import("@novnc/novnc");
  return resolveRfbConstructor(mod);
}

export class VncConsolePage {
  private readonly statusEl: HTMLElement;
  private readonly detailEl: HTMLElement;
  private readonly screenEl: HTMLElement;
  private readonly connectBtn: HTMLButtonElement;
  private readonly disconnectBtn: HTMLButtonElement;
  private rfb: RfbInstance | null = null;
  private readonly params: VncPageParams;
  private readonly RfbClass: RfbConstructor | null;
  private connecting = false;

  constructor(
    root: ParentNode = document,
    params: VncPageParams = readVncPageParams(),
    RfbClass: RfbConstructor | null = null,
  ) {
    this.params = params;
    this.RfbClass = RfbClass;
    this.statusEl = requireEl(root, "#vnc-status");
    this.detailEl = requireEl(root, "#vnc-detail");
    this.screenEl = requireEl(root, "#vnc-screen");
    this.connectBtn = requireEl(root, "#vnc-connect") as HTMLButtonElement;
    this.disconnectBtn = requireEl(root, "#vnc-disconnect") as HTMLButtonElement;

    this.connectBtn.addEventListener("click", () => {
      void this.connect();
    });
    this.disconnectBtn.addEventListener("click", () => {
      this.disconnect();
    });

    this.setStatus("idle", this.describeParams());
    // Auto-connect when all required ids are present (bookmarkable URL).
    if (params.workerId && params.hijackId && params.targetId) {
      void this.connect();
    }

    // Expose for e2e / Playwright.
    (window as unknown as { utermVnc?: VncConsolePage }).utermVnc = this;
  }

  get statusState(): VncStatusState {
    return (this.statusEl.dataset.state as VncStatusState) || "idle";
  }

  describeParams(): string {
    const p = this.params;
    if (!p.workerId || !p.hijackId || !p.targetId) {
      return "Missing worker_id, hijack_id, or target_id query params.";
    }
    return `target=${p.targetId} worker=${p.workerId} hijack=${p.hijackId.slice(0, 8)}…`;
  }

  setStatus(state: VncStatusState, message: string): void {
    this.statusEl.dataset.state = state;
    this.statusEl.textContent = message;
    this.detailEl.textContent = this.describeParams();
  }

  async connect(): Promise<void> {
    if (this.connecting) return;
    if (this.rfb) {
      this.disconnect();
    }
    let url: string;
    try {
      url = buildVncWsUrl(this.params);
    } catch (err) {
      this.setStatus("error", String(err));
      return;
    }

    this.connecting = true;
    this.setStatus("connecting", "Connecting…");
    this.connectBtn.disabled = true;
    this.disconnectBtn.disabled = false;
    this.screenEl.replaceChildren();

    try {
      const RfbClass = this.RfbClass ?? (await loadRfbClass());
      const options: Record<string, unknown> = {
        // Raw binary RFB over our relay (not a websockify subprotocol).
        wsProtocols: [],
      };
      const rfb = new RfbClass(this.screenEl, url, options);
      rfb.viewOnly = this.params.viewOnly;
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.background = "#000000";

      rfb.addEventListener("connect", () => {
        this.setStatus("connected", `Connected · ${this.params.targetId}`);
      });
      rfb.addEventListener("disconnect", (ev: Event) => {
        const detail = (ev as CustomEvent<{ clean?: boolean; code?: number }>).detail;
        const code = detail?.code;
        const info = statusFromCloseCode(code);
        if (code === undefined && detail?.clean) {
          this.setStatus("disconnected", "Disconnected");
        } else if (code === undefined) {
          this.setStatus("error", "Connection lost");
        } else {
          this.setStatus(info.state, info.message);
        }
        this.rfb = null;
        this.connectBtn.disabled = false;
        this.disconnectBtn.disabled = true;
      });
      rfb.addEventListener("securityfailure", () => {
        this.setStatus("error", "RFB security failure");
      });

      this.rfb = rfb;
    } catch (err) {
      this.setStatus("error", `Failed to start RFB client: ${String(err)}`);
      this.connectBtn.disabled = false;
      this.disconnectBtn.disabled = true;
    } finally {
      this.connecting = false;
    }
  }

  disconnect(): void {
    if (this.rfb) {
      try {
        this.rfb.disconnect();
      } catch {
        // ignore
      }
      this.rfb = null;
    }
    this.connectBtn.disabled = false;
    this.disconnectBtn.disabled = true;
    this.setStatus("disconnected", "Disconnected");
  }
}

function requireEl<T extends Element>(root: ParentNode, selector: string): T {
  const el = root.querySelector<T>(selector);
  if (el === null) {
    throw new Error(`Missing required element: ${selector}`);
  }
  return el;
}

function boot(): void {
  if (typeof document === "undefined") return;
  if (!document.getElementById("vnc-screen")) return;
  new VncConsolePage();
}

boot();
