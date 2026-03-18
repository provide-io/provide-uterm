//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

interface StatusBadgeProps {
  tone: "ok" | "error" | "info" | "warning" | "neutral";
  children: React.ReactNode;
  glow?: boolean;
}

interface ToneStyle {
  bg: string;
  color: string;
  glowColor?: string;
}

const NEUTRAL: ToneStyle = { bg: "transparent", color: "var(--text-tertiary)" };

const toneStyles: Record<StatusBadgeProps["tone"], ToneStyle> = {
  ok: { bg: "var(--bg-success)", color: "var(--text-success)", glowColor: "var(--success)" },
  error: { bg: "var(--bg-danger)", color: "var(--text-danger)", glowColor: "var(--danger)" },
  info: { bg: "var(--bg-info)", color: "var(--text-info)", glowColor: "var(--info)" },
  warning: { bg: "var(--bg-warning)", color: "var(--text-warning)", glowColor: "var(--warning)" },
  neutral: NEUTRAL,
};

export function StatusBadge({ tone, children, glow }: StatusBadgeProps) {
  const style = toneStyles[tone];
  return (
    <span style={{
      fontSize: 11,
      padding: "2px 8px",
      borderRadius: "var(--radius-pill)",
      background: style.bg,
      color: style.color,
      border: tone === "neutral" ? "0.5px solid var(--border-primary)" : "none",
      boxShadow: glow && style.glowColor ? `0 0 6px ${style.glowColor}` : undefined,
      whiteSpace: "nowrap",
    }}>
      {children}
    </span>
  );
}
