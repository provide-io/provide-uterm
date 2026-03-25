//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useEffect, useRef } from "react";
import type { SessionSurface } from "../../api/types";
import { useTerminalStore } from "../../stores/terminalStore";

declare global {
  interface Window {
    ProvideHijack?: new (
      container: HTMLElement,
      config: {
        workerId: string;
        showAnalysis?: boolean;
        mobileKeys?: boolean;
        onResize?: (cols: number, rows: number) => void;
      },
    ) => unknown;
  }
}

interface HijackHostProps {
  sessionId: string;
  surface?: SessionSurface;
}

export function HijackHost({ sessionId, surface }: HijackHostProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(false);
  const setMounted = useTerminalStore((s) => s.setMounted);
  const setDimensions = useTerminalStore((s) => s.setDimensions);

  useEffect(() => {
    if (mountedRef.current || !containerRef.current) return;
    const HijackCtor = window.ProvideHijack;
    if (typeof HijackCtor !== "function") {
      setMounted(false, "ProvideHijack is not available — ensure hijack.js is loaded");
      return;
    }
    const isOperator = surface === "operator";
    mountedRef.current = true;
    setMounted(true);
    new HijackCtor(containerRef.current, {
      workerId: sessionId,
      showAnalysis: isOperator,
      mobileKeys: isOperator,
      onResize: (cols, rows) => setDimensions(cols, rows),
    });
  }, [sessionId, surface, setMounted, setDimensions]);

  return <div ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}
