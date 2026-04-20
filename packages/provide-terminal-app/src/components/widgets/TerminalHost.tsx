//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useEffect, useRef } from "react";
import { useTerminalStore } from "../../stores/terminalStore";

declare global {
  interface Window {
    ProvideTerminal?: new (container: HTMLElement, config: Record<string, unknown>) => { dispose(): void };
  }
}

interface TerminalHostProps {
  config?: Record<string, unknown>;
}

export function TerminalHost({ config }: TerminalHostProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(false);
  const setMounted = useTerminalStore((s) => s.setMounted);

  useEffect(() => {
    if (mountedRef.current || !containerRef.current) return;
    const TermWidget = window.ProvideTerminal;
    if (typeof TermWidget !== "function") {
      setMounted(false, "ProvideTerminal is not available");
      return;
    }
    const widget = new TermWidget(containerRef.current, config ?? {});
    mountedRef.current = true;
    setMounted(true);
    return () => { widget.dispose(); };
  }, [config, setMounted]);

  return <div ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}
