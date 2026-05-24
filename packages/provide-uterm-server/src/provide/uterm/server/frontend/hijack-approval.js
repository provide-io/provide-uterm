//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
/**
 * Approval-UI HTML builders split out of hijack.ts. Pure functions: they take
 * input and return string markup with no widget-state dependency.
 */
import { escapeHijackHtml } from "./hijack-ui.js";
export function buildApprovalModalHtml(opts) {
    const actions = opts.isAdmin
        ? `
          <div class="hijack-approval-actions">
            <button class="hijack-btn hijack-btn-approve" id="h-${opts.uid}-approve">Approve</button>
            <button class="hijack-btn hijack-btn-reject" id="h-${opts.uid}-reject">Reject</button>
          </div>`
        : "";
    return `
        <div class="hijack-approval-card">
          <div class="hijack-approval-title">⚠️ APPROVAL REQUIRED</div>
          <div class="hijack-approval-body">
            Your command is being held for administrative review.
            <div class="hijack-approval-command">${escapeHijackHtml(opts.command)}</div>
            <div class="hijack-approval-timer">Expires in <span id="h-${opts.uid}-approval-timer">--</span>s...</div>
          </div>
          ${actions}
        </div>
      `;
}
export function buildApprovalStatusBarHtml(opts) {
    return `
        <div class="hijack-approval-status">
          <span class="hijack-approval-spinner">⏳</span>
          PAUSED: Command pending approval (<span id="h-${opts.uid}-approval-timer">--</span>s)
        </div>
      `;
}
export function approvalElementClass(mode) {
    return mode === "modal" ? "hijack-approval-modal" : "hijack-approval-statusbar";
}
export function computeRemainingSeconds(expiresAt, nowMs = Date.now()) {
    return Math.max(0, Math.round(expiresAt - nowMs / 1000));
}
