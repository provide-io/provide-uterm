//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

export interface DeckMuxUser {
  userId: string;
  name: string;
  color: string;
  role: string;
  initials: string;
  scrollLine: number;
  scrollRange: [number, number];
  cols: number; // terminal width (0 = unknown)
  rows: number; // terminal height (0 = unknown)
  joinTime: number; // Date.now() ms when first seen
  selection: { start: { line: number; col: number }; end: { line: number; col: number } } | null;
  pin: { line: number } | null;
  typing: boolean;
  queuedKeys: string;
  isOwner: boolean;
}

export interface DeckMuxConfig {
  autoTransferIdleS: number;
  keystrokeQueue: "display" | "replay";
  ghostBox?: boolean; // default true — show ghost box overlay
}

export interface ContextAction {
  icon: string;
  label: string;
  sublabel?: string;
  danger?: boolean;
  onClick: () => void;
}
