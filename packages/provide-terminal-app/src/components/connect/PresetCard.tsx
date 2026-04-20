//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

interface PresetCardProps {
  connection: {
    host: string;
    transport: string;
    port: number;
    lastUsed: string;
  };
}

function formatTimeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const hours = Math.floor(diff / 3600000);
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function PresetCard({ connection }: PresetCardProps) {
  return (
    <div className="preset-card">
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
        <div className="status-dot" style={{ background: "var(--text-tertiary)" }} />
        <span style={{ fontSize: 13, fontWeight: 500 }}>{connection.host}</span>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
        {connection.transport}:{connection.port} · last used {formatTimeAgo(connection.lastUsed)}
      </div>
    </div>
  );
}
