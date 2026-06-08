//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Theme palette and pure helpers for ProvideTerminal: theme name validation,
 * per-theme effect defaults, and DOM-class application. Kept free of the
 * widget class so the same helpers can be unit-tested without instantiation.
 */

export type ThemeName = "crt" | "bbs" | "glass" | "code";

export interface ThemeEffectFlags {
  scanlines: boolean;
  vignette: boolean;
  glow: boolean;
}

export const THEME_DEFAULTS: Record<ThemeName, ThemeEffectFlags> = {
  crt: { scanlines: true, vignette: true, glow: false },
  bbs: { scanlines: false, vignette: false, glow: false },
  glass: { scanlines: false, vignette: false, glow: true },
  code: { scanlines: false, vignette: false, glow: false },
};

export function asThemeName(value: string | null | undefined): ThemeName {
  return value === "bbs" || value === "glass" || value === "crt" ? value : "code";
}

/**
 * Apply theme + effect classes to a root element, replacing any previous theme.
 * The widget calls this on initial render and after every settings change.
 */
export function applyThemeClasses(root: HTMLElement, theme: ThemeName, effects: ThemeEffectFlags): void {
  root.classList.remove(
    "theme-crt",
    "theme-bbs",
    "theme-glass",
    "theme-code",
    "fx-scanlines",
    "fx-vignette",
    "fx-glow",
  );
  root.classList.add(`theme-${theme}`);
  if (effects.scanlines) root.classList.add("fx-scanlines");
  if (effects.vignette) root.classList.add("fx-vignette");
  if (effects.glow) root.classList.add("fx-glow");
}

/** Page + terminal background colors applied as CSS custom properties. */
export function applyColors(root: HTMLElement, pageBg: string, termBg: string): void {
  root.style.setProperty("--bg-page", pageBg);
  root.style.setProperty("--bg-terminal", termBg);
  root.style.background = pageBg;
}

import { html, type TemplateResult } from "lit";

/** HTML fragment for the theme-picker row in the settings panel. */
export function buildThemeButtonsHtml(): TemplateResult {
  return html`
        <div class="theme-options" role="group" aria-label="Theme">
          <button type="button" class="theme-btn" data-theme="code" aria-label="Code theme">Code</button>
          <button type="button" class="theme-btn" data-theme="crt" aria-label="CRT theme">CRT</button>
          <button type="button" class="theme-btn" data-theme="bbs" aria-label="BBS/DOS theme">BBS/DOS</button>
          <button type="button" class="theme-btn" data-theme="glass" aria-label="Glass theme">Glass</button>
        </div>`;
}
