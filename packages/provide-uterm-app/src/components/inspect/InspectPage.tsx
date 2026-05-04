//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useEffect, useMemo } from "react";
import type { AppBootstrap } from "../../api/types";
import { useInspectStore } from "../../stores/inspectStore";
import { AppHeader } from "../layout/AppHeader";
import { PageShell } from "../layout/PageShell";
import { InspectDetail } from "./InspectDetail";
import { InspectList } from "./InspectList";
import { InspectToolbar } from "./InspectToolbar";
import { useInspectWs } from "./useInspectWs";

interface InspectPageProps {
  bootstrap: AppBootstrap;
}

export function InspectPage({ bootstrap }: InspectPageProps) {
  const sessionId = bootstrap.session_id;
  if (!sessionId) throw new Error("inspect bootstrap missing session_id");

  const {
    exchanges, selected, methodFilter, urlFilter,
    inspectEnabled, interceptEnabled,
    select, resolveIntercept, setInspectEnabled, setInterceptEnabled, clear,
  } = useInspectStore();

  const { sendJson } = useInspectWs(sessionId);

  useEffect(() => {
    return () => { clear(); };
  }, [clear]);

  const filtered = useMemo(() => {
    const mf = methodFilter;
    const uf = urlFilter.toLowerCase();
    return exchanges.filter((ex) => {
      if (mf && ex.request.method !== mf) return false;
      if (uf && !ex.request.url.toLowerCase().includes(uf)) return false;
      return true;
    });
  }, [exchanges, methodFilter, urlFilter]);

  const selectedExchange = useMemo(
    () => exchanges.find((ex) => ex.id === selected) ?? null,
    [exchanges, selected],
  );

  function handleToggleInspect() {
    const next = !inspectEnabled;
    setInspectEnabled(next);
    sendJson({ type: "http_inspect_toggle", enabled: next });
    if (!next) {
      setInterceptEnabled(false);
      sendJson({ type: "http_intercept_toggle", enabled: false });
    }
  }

  function handleToggleIntercept() {
    const next = !interceptEnabled;
    setInterceptEnabled(next);
    sendJson({ type: "http_intercept_toggle", enabled: next });
  }

  function handleAction(id: string, action: string, headers?: Record<string, string>, bodyB64?: string) {
    const msg: Record<string, unknown> = { type: "http_action", id, action };
    if (headers) msg.headers = headers;
    if (bodyB64) msg.body_b64 = bodyB64;
    sendJson(msg);
    resolveIntercept(id, action);
  }

  return (
    <PageShell>
      <AppHeader
        bootstrap={bootstrap}
        crumbs={[{ label: "Inspect" }]}
      />
      <div style={{ display: "flex", flexDirection: "column", flex: 1, overflow: "hidden", padding: "0 16px" }}>
        <InspectToolbar
          onToggleInspect={handleToggleInspect}
          onToggleIntercept={handleToggleIntercept}
          filteredCount={filtered.length}
        />
        <div style={{ display: "flex", flex: 1, overflow: "hidden", gap: 1, border: "1px solid var(--border-primary, #333)", borderRadius: 6 }}>
          <div style={{ flex: 1, display: "flex", flexDirection: "column", borderRight: "1px solid var(--border-primary, #333)" }}>
            <InspectList exchanges={filtered} selected={selected} onSelect={select} />
          </div>
          <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
            <InspectDetail exchange={selectedExchange} onAction={handleAction} />
          </div>
        </div>
      </div>
    </PageShell>
  );
}
