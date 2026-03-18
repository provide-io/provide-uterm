//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useState } from "react";
import type { RecordingEntryView } from "../../api/types";
import { ansiToSegments } from "../../utils/ansiToHtml";

interface ScreenPreviewProps {
  entry: RecordingEntryView | null;
  index: number;
}

export function ScreenPreview({ entry, index }: ScreenPreviewProps) {
  const [rendered, setRendered] = useState(true);

  if (!entry) {
    return <div style={{ color: "var(--text-secondary)", fontFamily: "var(--font-sans)" }}>No entries loaded.</div>;
  }

  const ts = entry.ts != null ? formatTime(entry.ts) : "—";
  const screen = entry.screen || "(no screen data)";

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <span className="screen-caption" style={{ marginBottom: 0 }}>
          Screen snapshot at event #{index + 1} ({ts})
        </span>
        <div className="flex-spacer" />
        <div style={{ display: "flex", gap: 2 }}>
          <button
            type="button"
            className={rendered ? "btn-primary" : ""}
            style={{ fontSize: 10, padding: "2px 8px", borderRadius: "var(--radius-sm)" }}
            onClick={() => setRendered(true)}
          >
            Rendered
          </button>
          <button
            type="button"
            className={!rendered ? "btn-primary" : ""}
            style={{ fontSize: 10, padding: "2px 8px", borderRadius: "var(--radius-sm)" }}
            onClick={() => setRendered(false)}
          >
            Raw
          </button>
        </div>
      </div>
      {rendered ? <AnsiPre text={screen} /> : <pre className="pre-block">{screen}</pre>}
    </>
  );
}

function AnsiPre({ text }: { text: string }) {
  // biome-ignore lint/suspicious/noControlCharactersInRegex: ANSI ESC detection
  const hasAnsi = /\x1b\[/.test(text);
  if (!hasAnsi) {
    return <pre className="pre-block">{text}</pre>;
  }
  const segments = ansiToSegments(text);
  return (
    <pre className="pre-block">
      {segments.map((seg, i) => {
        const key = `${i}-${seg.text.length}`;
        return seg.style ? <span key={key} style={parseInlineStyle(seg.style)}>{seg.text}</span> : seg.text;
      })}
    </pre>
  );
}

function parseInlineStyle(css: string): React.CSSProperties {
  const style: Record<string, string> = {};
  for (const decl of css.split(";")) {
    const [prop, val] = decl.split(":");
    if (prop && val) {
      style[prop.replace(/-([a-z])/g, (_, c: string) => c.toUpperCase())] = val;
    }
  }
  return style;
}

function formatTime(ts: number): string {
  const h = Math.floor(ts / 3600);
  const m = Math.floor((ts % 3600) / 60);
  const s = Math.floor(ts % 60);
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
