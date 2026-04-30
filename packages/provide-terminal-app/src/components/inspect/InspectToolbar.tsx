//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useInspectStore } from "../../stores/inspectStore";
import { StatusBadge } from "../common/StatusBadge";

const METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"];

interface InspectToolbarProps {
  onToggleInspect: () => void;
  onToggleIntercept: () => void;
  filteredCount: number;
}

export function InspectToolbar({ onToggleInspect, onToggleIntercept, filteredCount }: InspectToolbarProps) {
  const { methodFilter, urlFilter, inspectEnabled, interceptEnabled, wsStatus, setMethodFilter, setUrlFilter } =
    useInspectStore();

  const statusTone = wsStatus === "connected" ? "ok" : wsStatus === "disconnected" ? "error" : "info";
  const statusLabel = wsStatus === "connected" ? "Connected" : wsStatus === "disconnected" ? "Disconnected" : "Connecting…";

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", padding: "8px 0", flexWrap: "wrap" }}>
      <select value={methodFilter} onChange={(e) => setMethodFilter(e.target.value)} style={{ padding: "4px 8px" }}>
        <option value="">All Methods</option>
        {METHODS.map((m) => (
          <option key={m}>{m}</option>
        ))}
      </select>
      <input
        type="text"
        placeholder="Filter URL..."
        value={urlFilter}
        onChange={(e) => setUrlFilter(e.target.value)}
        style={{ padding: "4px 8px", flex: 1, minWidth: 120 }}
      />
      <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>
        {filteredCount} request{filteredCount !== 1 ? "s" : ""}
      </span>
      <button
        type="button"
        onClick={onToggleInspect}
        style={{ padding: "4px 12px", fontWeight: inspectEnabled ? 700 : 400 }}
      >
        Inspect: {inspectEnabled ? "ON" : "OFF"}
      </button>
      <button
        type="button"
        onClick={onToggleIntercept}
        style={{ padding: "4px 12px", fontWeight: interceptEnabled ? 700 : 400 }}
      >
        Intercept: {interceptEnabled ? "ON" : "OFF"}
      </button>
      <StatusBadge tone={statusTone}>{statusLabel}</StatusBadge>
    </div>
  );
}
