//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { SessionMode } from "../../api/types";

interface ModeToggleProps {
  mode: SessionMode;
  disabled?: boolean;
  onChange: (mode: SessionMode) => void;
}

export function ModeToggle({ mode, disabled, onChange }: ModeToggleProps) {
  return (
    <div style={{
      display: "flex",
      gap: 4,
      background: "var(--bg-tertiary)",
      borderRadius: "var(--radius-md)",
      padding: 3,
    }}>
      <ToggleOption
        label="Shared"
        active={mode === "open"}
        disabled={disabled}
        onClick={() => onChange("open")}
      />
      <ToggleOption
        label="Exclusive"
        active={mode === "hijack"}
        disabled={disabled}
        onClick={() => onChange("hijack")}
      />
    </div>
  );
}

function ToggleOption({
  label,
  active,
  disabled,
  onClick,
}: {
  label: string;
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      style={{
        flex: 1,
        textAlign: "center",
        padding: 6,
        borderRadius: 6,
        fontSize: 12,
        fontWeight: active ? 500 : 400,
        background: active ? "var(--bg-primary)" : "transparent",
        border: active ? "0.5px solid var(--border-primary)" : "none",
        color: active ? "var(--text-primary)" : "var(--text-secondary)",
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      {label}
    </button>
  );
}
