//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useEffect, useRef } from "react";
import type { AppBootstrap } from "./api/types";
import { ConnectPage } from "./components/connect/ConnectPage";
import { DashboardPage } from "./components/dashboard/DashboardPage";
import { OperatorPage } from "./components/operator/OperatorPage";
import { ReplayPage } from "./components/replay/ReplayPage";
import { SessionPage } from "./components/session/SessionPage";
import { ErrorBoundary } from "./components/common/ErrorBoundary";

/** Wrapper that delegates to the vanilla TS inspect view. */
function InspectPage({ bootstrap }: { bootstrap: AppBootstrap }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    import("@provide-terminal-frontend/app/views/inspect-view").then(({ renderInspect }) => {
      renderInspect(ref.current!, bootstrap as Parameters<typeof renderInspect>[1]);
    });
  }, [bootstrap]);
  return <div ref={ref} />;
}

interface AppProps {
  bootstrap: AppBootstrap;
}

export function App({ bootstrap }: AppProps) {
  return (
    <ErrorBoundary>
      {(() => {
        switch (bootstrap.page_kind) {
          case "dashboard":
            return <DashboardPage bootstrap={bootstrap} />;
          case "connect":
            return <ConnectPage bootstrap={bootstrap} />;
          case "operator":
            return <OperatorPage bootstrap={bootstrap} />;
          case "session":
            return <SessionPage bootstrap={bootstrap} />;
          case "replay":
            return <ReplayPage bootstrap={bootstrap} />;
          case "inspect":
            return <InspectPage bootstrap={bootstrap} />;
        }
      })()}
    </ErrorBoundary>
  );
}
