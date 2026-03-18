//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

interface MetricCardProps {
  label: string;
  value: number;
  color?: string;
}

export function MetricCard({ label, value, color }: MetricCardProps) {
  return (
    <div className="card">
      <div className="card-label">{label}</div>
      <div className="card-value" style={{ color: color ?? "var(--text-primary)" }}>{value}</div>
    </div>
  );
}
