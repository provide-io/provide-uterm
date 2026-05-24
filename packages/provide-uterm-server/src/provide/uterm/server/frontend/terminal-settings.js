//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/**
 * Settings persistence and panel-HTML builder for ProvideTerminal. The widget
 * class owns the panel's event wiring; this module owns the data model,
 * defaults, localStorage round-tripping, and DOM markup.
 */
import { asThemeName, buildThemeButtonsHtml } from "./terminal-themes.js";
export const DEFAULTS = {
    theme: "code",
    cols: 80,
    rows: 25,
    fontSize: 14,
    pageBg: "#0a0a0a",
    termBg: "#0a0a0a",
    scanlines: true,
    vignette: true,
    glow: false,
    storageKey: "provide-uterm-settings",
    title: null,
};
/**
 * Merge constructor config over DEFAULTS, then layer any persisted settings
 * from localStorage on top. localStorage parse errors silently fall back to
 * the config-only baseline so a corrupt entry can never wedge startup.
 */
export function loadSettings(config) {
    const base = { ...DEFAULTS, ...config, theme: asThemeName(config.theme) };
    try {
        const raw = localStorage.getItem(config.storageKey);
        if (!raw)
            return base;
        const parsed = JSON.parse(raw);
        return {
            ...base,
            ...parsed,
            theme: asThemeName(parsed.theme ?? base.theme),
        };
    }
    catch {
        return base;
    }
}
export function saveSettings(settings) {
    localStorage.setItem(settings.storageKey, JSON.stringify(settings));
}
/** Gear icon SVG used inside the settings-panel toggle button. */
export const GEAR_SVG = '<svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.49.49 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.48.48 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 00-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6A3.6 3.6 0 1112 8.4a3.6 3.6 0 010 7.2z"/></svg>';
/**
 * Build the gear-button + overlay + settings-panel HTML for a given widget
 * instance. The {uid} suffix on every element ID lets multiple widgets coexist
 * on the same page without selector collisions.
 */
export function buildSettingsPanelHtml(uid) {
    return `
      <button type="button" class="gear-btn" id="gearBtn-${uid}" title="Settings" aria-label="Open terminal settings">${GEAR_SVG}</button>
      <div class="settings-overlay" id="settingsOverlay-${uid}"></div>
      <div class="settings-panel" id="settingsPanel-${uid}" role="dialog" aria-label="Terminal settings">
        <h3>Theme</h3>${buildThemeButtonsHtml()}
        <h3>Terminal Size</h3>
        <div class="setting-row">
          <label>Columns</label>
          <input type="range" id="setCols-${uid}" min="80" max="120" value="80">
          <span class="val" id="valCols-${uid}">80</span>
        </div>
        <div class="setting-row">
          <label>Rows</label>
          <input type="range" id="setRows-${uid}" min="25" max="40" value="25">
          <span class="val" id="valRows-${uid}">25</span>
        </div>
        <div class="setting-row">
          <label>Font Size</label>
          <input type="range" id="setFontSize-${uid}" min="11" max="18" value="14">
          <span class="val" id="valFontSize-${uid}">14px</span>
        </div>
        <h3>Colors</h3>
        <div class="setting-row">
          <label>Page Background</label>
          <input type="color" id="setPageBg-${uid}" value="#0a0a0a">
        </div>
        <div class="setting-row">
          <label>Terminal Background</label>
          <input type="color" id="setTermBg-${uid}" value="#0a0a0a">
        </div>
        <h3>Effects</h3>
        <div class="setting-row">
          <label>Scanlines</label>
          <input type="checkbox" id="fxScanlines-${uid}">
        </div>
        <div class="setting-row">
          <label>Vignette</label>
          <input type="checkbox" id="fxVignette-${uid}">
        </div>
        <div class="setting-row">
          <label>Glow</label>
          <input type="checkbox" id="fxGlow-${uid}">
        </div>
      </div>`;
}
