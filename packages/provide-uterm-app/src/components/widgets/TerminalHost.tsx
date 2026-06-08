//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { DetailedHTMLProps, HTMLAttributes, Ref } from "react";
import { useEffect, useRef } from "react";
import type { TerminalElement } from "@provide-uterm-frontend/terminal-element";
import { useTerminalStore } from "../../stores/terminalStore";

declare module "react" {
  namespace JSX {
    interface IntrinsicElements {
      "uterm-terminal": DetailedHTMLProps<HTMLAttributes<HTMLElement>, HTMLElement> & {
        config?: unknown;
        ref?: Ref<TerminalElement>;
      };
    }
  }
}

interface TerminalHostProps {
  config?: Record<string, unknown>;
}

export function TerminalHost({ config }: TerminalHostProps) {
  const containerRef = useRef<TerminalElement>(null);
  const mountedRef = useRef(false);
  const setMounted = useTerminalStore((s) => s.setMounted);

  useEffect(() => {
    if (mountedRef.current || !containerRef.current) return;
    mountedRef.current = true;
    setMounted(true);
    const widget = containerRef.current;
    widget.config = config ?? {};
    widget.connect();
    
    return () => {
      mountedRef.current = false;
    };
  }, [config, setMounted]);

  return <uterm-terminal ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}
