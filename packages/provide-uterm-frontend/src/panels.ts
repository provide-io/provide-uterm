//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/**
 * Recursive VNC + terminal tiling page ("nautilus" panels).
 *
 * Starts as a VNC | terminal split screen; every pane can split again and the
 * whole layout can spiral (golden-ratio nautilus). Pure layout math lives in
 * panels-model.ts; this module owns the DOM and the live connections.
 *
 * URL params:
 *   vnc  = comma list of `workerId~hijackId~targetId` triples (VNC pane pool)
 *   term = comma list of `workerId~role` pairs (terminal pane pool; role=browser)
 *   Panes round-robin through each pool, so one entry = shared, many = fan-out.
 */

import "./terminal-element.js"; // registers the <uterm-terminal> custom element
import {
  closeLeaf,
  collectLeaves,
  type IdGen,
  initialLayout,
  type Leaf,
  makeIdGen,
  type Node,
  nautilusAdd,
  splitLeaf,
} from "./panels-model.js";
import { attachVnc, type VncAttachHandle } from "./vnc-page.js";

interface VncSource {
  workerId: string;
  hijackId: string;
  targetId: string;
}
interface TermSource {
  workerId: string;
  role: string;
}

interface MountedPane {
  el: HTMLElement;
  dispose: () => void;
}

function readPools(search: string): { vnc: VncSource[]; term: TermSource[] } {
  const q = new URLSearchParams(search);
  const vnc = (q.get("vnc") ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((triple) => {
      const [workerId, hijackId, targetId] = triple.split("~");
      return { workerId, hijackId, targetId } as VncSource;
    })
    .filter((v) => v.workerId && v.hijackId && v.targetId);
  const term = (q.get("term") ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((pair) => {
      const [workerId, role] = pair.split("~");
      return { workerId, role: role || "browser" } as TermSource;
    })
    .filter((t) => t.workerId);
  return { vnc, term };
}

export class PanelsPage {
  private layout: Node;
  private readonly mkId: IdGen = makeIdGen("p");
  private nautilusStep = 0;
  private readonly panes = new Map<string, MountedPane>();
  private readonly stage: HTMLElement;
  private readonly countEl: HTMLElement | null;
  private vncCursor = 0;
  private termCursor = 0;
  private readonly vncPool: VncSource[];
  private readonly termPool: TermSource[];

  constructor(stage: HTMLElement, search: string = window.location.search) {
    this.stage = stage;
    this.countEl = document.getElementById("panels-count");
    const pools = readPools(search);
    this.vncPool = pools.vnc;
    this.termPool = pools.term;
    this.layout = initialLayout(this.mkId);
    this.render();
    (window as unknown as { utermPanels?: PanelsPage }).utermPanels = this;
  }

  get leafCount(): number {
    return collectLeaves(this.layout).length;
  }

  splitPane(leafId: string): void {
    this.layout = splitLeaf(this.layout, leafId, this.mkId);
    this.render();
  }

  closePane(leafId: string): void {
    const next = closeLeaf(this.layout, leafId);
    if (next === null) return; // never remove the last pane
    this.layout = next;
    this.render();
  }

  nautilus(): void {
    this.layout = nautilusAdd(this.layout, this.mkId, this.nautilusStep++);
    this.render();
  }

  private nextVnc(): VncSource | null {
    if (this.vncPool.length === 0) return null;
    const s = this.vncPool[this.vncCursor % this.vncPool.length] ?? null;
    this.vncCursor++;
    return s;
  }

  private nextTerm(): TermSource | null {
    if (this.termPool.length === 0) return null;
    const s = this.termPool[this.termCursor % this.termPool.length] ?? null;
    this.termCursor++;
    return s;
  }

  private mountVnc(leaf: Leaf): MountedPane {
    const el = document.createElement("div");
    el.className = "pane pane-vnc";
    el.dataset.leaf = leaf.id;
    const bar = this.paneBar(leaf, "VNC");
    const screen = document.createElement("div");
    screen.className = "pane-screen";
    el.append(bar, screen);
    const src = this.nextVnc();
    let handle: VncAttachHandle | null = null;
    const badge = bar.querySelector<HTMLElement>(".pane-badge");
    if (src) {
      handle = attachVnc(
        screen,
        { workerId: src.workerId, hijackId: src.hijackId, targetId: src.targetId, viewOnly: false, token: null },
        {
          onStatus: (state, msg) => {
            el.dataset.state = state;
            if (badge) badge.textContent = msg;
          },
        },
      );
    } else if (badge) {
      badge.textContent = "no vnc source";
    }
    return { el, dispose: () => handle?.disconnect() };
  }

  private mountTerminal(leaf: Leaf): MountedPane {
    const el = document.createElement("div");
    el.className = "pane pane-term";
    el.dataset.leaf = leaf.id;
    const bar = this.paneBar(leaf, "TERM");
    const host = document.createElement("div");
    host.className = "pane-screen";
    el.append(bar, host);
    const src = this.nextTerm();
    const badge = bar.querySelector<HTMLElement>(".pane-badge");
    if (src) {
      // uterm-terminal's own ResizeObserver shrinks the font (min 6px) to keep
      // ~80 columns fitting, so as the nautilus panes get smaller the text
      // scales down and the whole transcript stays visible — no CSS transform
      // (which breaks xterm's canvas measurement).
      const w = document.createElement("uterm-terminal") as unknown as {
        config: Record<string, unknown>;
        connect?: () => void;
      };
      w.config = { wsUrl: `/ws/${src.role}/${src.workerId}/term`, title: src.workerId };
      host.appendChild(w as unknown as HTMLElement);
      if (badge) badge.textContent = src.workerId;
      window.setTimeout(() => w.connect?.(), 0);
    } else if (badge) {
      badge.textContent = "no term source";
    }
    return { el, dispose: () => {} };
  }

  private paneBar(leaf: Leaf, kind: string): HTMLElement {
    const bar = document.createElement("div");
    bar.className = "pane-bar";
    const label = document.createElement("span");
    label.className = "pane-kind";
    label.textContent = kind;
    const badge = document.createElement("span");
    badge.className = "pane-badge";
    const spacer = document.createElement("span");
    spacer.className = "pane-spacer";
    const splitBtn = document.createElement("button");
    splitBtn.className = "pane-btn";
    splitBtn.title = "Split this pane";
    splitBtn.textContent = "⊞";
    splitBtn.addEventListener("click", () => this.splitPane(leaf.id));
    const closeBtn = document.createElement("button");
    closeBtn.className = "pane-btn";
    closeBtn.title = "Close this pane";
    closeBtn.textContent = "×";
    closeBtn.addEventListener("click", () => this.closePane(leaf.id));
    bar.append(label, badge, spacer, splitBtn, closeBtn);
    return bar;
  }

  private paneFor(leaf: Leaf): HTMLElement {
    let mounted = this.panes.get(leaf.id);
    if (!mounted) {
      // A terminal leaf with no terminal source but a VNC pool available renders
      // as a VNC-of-terminal (noVNC scale-viewport shrinks the desktop, so its
      // font shrinks with the pane) — used when every pane should show the same
      // live terminal through the relay.
      const asVnc = leaf.pane === "vnc" || (this.termPool.length === 0 && this.vncPool.length > 0);
      mounted = asVnc ? this.mountVnc(leaf) : this.mountTerminal(leaf);
      this.panes.set(leaf.id, mounted);
    }
    return mounted.el;
  }

  private buildNode(node: Node): HTMLElement {
    if (node.kind === "leaf") {
      return this.paneFor(node);
    }
    const box = document.createElement("div");
    box.className = `split split-${node.dir}`;
    const a = document.createElement("div");
    a.className = "split-cell";
    a.style.flex = `${node.ratio}`;
    a.appendChild(this.buildNode(node.first));
    const b = document.createElement("div");
    b.className = "split-cell";
    b.style.flex = `${1 - node.ratio}`;
    b.appendChild(this.buildNode(node.second));
    box.append(a, b);
    return box;
  }

  private render(): void {
    // Dispose panes whose leaves no longer exist (moved-out DOM keeps live ones).
    const live = new Set(collectLeaves(this.layout).map((l) => l.id));
    for (const [id, pane] of this.panes) {
      if (!live.has(id)) {
        pane.dispose();
        this.panes.delete(id);
      }
    }
    const tree = this.buildNode(this.layout);
    this.stage.replaceChildren(tree);
    if (this.countEl) this.countEl.textContent = String(this.leafCount);
  }
}

function boot(): void {
  if (typeof document === "undefined") return;
  const stage = document.getElementById("panels-stage");
  if (!stage) return;
  const page = new PanelsPage(stage);
  document.getElementById("panels-nautilus")?.addEventListener("click", () => page.nautilus());
}

boot();
