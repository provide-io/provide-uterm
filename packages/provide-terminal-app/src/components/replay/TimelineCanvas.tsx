//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useCallback, useEffect, useRef } from "react";
import { useReplayStore } from "../../stores/replayStore";

const EVENT_COLORS: Record<string, string> = {
  read: "#1D9E75",
  send: "#378ADD",
  runtime_error: "#E24B4A",
  runtime_started: "#BA7517",
  log_start: "#BA7517",
  log_stop: "#BA7517",
};

const DEFAULT_COLOR = "#1D9E75";
const BAR_WIDTH = 2;
const BAR_GAP = 1;
const CANVAS_HEIGHT = 48;

export function TimelineCanvas() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const { entries, index, setIndex } = useReplayStore();

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = CANVAS_HEIGHT * dpr;
    ctx.scale(dpr, dpr);
    canvas.style.height = `${CANVAS_HEIGHT}px`;

    // Background
    ctx.fillStyle = getComputedStyle(canvas).getPropertyValue("--bg-secondary").trim() || "#151b22";
    ctx.fillRect(0, 0, rect.width, CANVAS_HEIGHT);

    if (entries.length === 0) return;

    const totalBarWidth = BAR_WIDTH + BAR_GAP;
    const maxBars = Math.floor((rect.width - 8) / totalBarWidth);
    const step = entries.length > maxBars ? entries.length / maxBars : 1;
    const barCount = Math.min(entries.length, maxBars);
    const startX = 4;

    // Draw bars
    for (let i = 0; i < barCount; i++) {
      const entryIdx = Math.min(Math.floor(i * step), entries.length - 1);
      const entry = entries[entryIdx];
      if (!entry) continue;
      const color = EVENT_COLORS[entry.event] ?? DEFAULT_COLOR;
      // Height based on data size or fixed
      const dataSize = entry.payload ? JSON.stringify(entry.payload).length : 0;
      const h = Math.max(6, Math.min(40, 6 + Math.log2(dataSize + 1) * 4));
      const x = startX + i * totalBarWidth;
      const y = CANVAS_HEIGHT - 4 - h;

      ctx.globalAlpha = 0.6;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.roundRect(x, y, BAR_WIDTH, h, 1);
      ctx.fill();
    }

    // Draw seek indicator
    const seekX = startX + (index / Math.max(1, entries.length - 1)) * (barCount - 1) * totalBarWidth;
    ctx.globalAlpha = 0.8;
    ctx.fillStyle = getComputedStyle(canvas).getPropertyValue("--text-info").trim() || "#58a6ff";
    ctx.fillRect(seekX, 0, 2, CANVAS_HEIGHT);
    ctx.globalAlpha = 1;

    // Time labels
    ctx.fillStyle = getComputedStyle(canvas).getPropertyValue("--text-tertiary").trim() || "#484f58";
    ctx.font = "10px sans-serif";
    const firstTs = entries[0]?.ts;
    const lastTs = entries[entries.length - 1]?.ts;
    if (firstTs != null) {
      ctx.fillText(formatTime(0), 4, CANVAS_HEIGHT - 4);
    }
    if (firstTs != null && lastTs != null) {
      const duration = lastTs - firstTs;
      ctx.textAlign = "right";
      ctx.fillText(formatTime(duration), rect.width - 4, CANVAS_HEIGHT - 4);
      ctx.textAlign = "left";
    }
  }, [entries, index]);

  useEffect(() => {
    draw();
    const handleResize = () => draw();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [draw]);

  function handleClick(e: React.MouseEvent<HTMLCanvasElement>) {
    const canvas = canvasRef.current;
    if (!canvas || entries.length === 0) return;
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const fraction = x / rect.width;
    setIndex(Math.round(fraction * (entries.length - 1)));
  }

  return (
    <canvas
      ref={canvasRef}
      onClick={handleClick}
      style={{
        width: "100%",
        height: CANVAS_HEIGHT,
        borderRadius: "var(--radius-md)",
        cursor: "pointer",
        display: "block",
      }}
    />
  );
}

function formatTime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}
