//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { AppBootstrap } from "./api/types";
import { ConnectPage } from "./components/connect/ConnectPage";
import { DashboardPage } from "./components/dashboard/DashboardPage";
import { InspectPage } from "./components/inspect/InspectPage";
import { OperatorPage } from "./components/operator/OperatorPage";
import { ReplayPage } from "./components/replay/ReplayPage";
import { SessionPage } from "./components/session/SessionPage";
import { ErrorBoundary } from "./components/common/ErrorBoundary";
import type { UtermExtensionRegistry } from "./extensions";

export interface AppProps {
  bootstrap: AppBootstrap;
  extensions?: UtermExtensionRegistry;
}

/** Rendered when `page_kind` has no matching case; throws so the nearest
 *  ErrorBoundary can display its fallback rather than silently showing nothing. */
function UnknownPageKind({ kind }: { kind: string }): never {
  throw new Error(`Unknown page_kind: ${kind}`);
}

export function App({ bootstrap, extensions }: AppProps) {
  const consumerPage = extensions?.resolvePage(bootstrap.page_kind);
  const ConsumerPage = consumerPage?.component;
  return (
    <ErrorBoundary>
      {(() => {
        if (ConsumerPage) return <ConsumerPage bootstrap={bootstrap} />;
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
          default:
            return <UnknownPageKind kind={bootstrap.page_kind} />;
        }
      })()}
    </ErrorBoundary>
  );
}
