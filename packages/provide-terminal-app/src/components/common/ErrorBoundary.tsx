//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { Component, type ErrorInfo, type ReactNode } from "react";
import { PageShell } from "../layout/PageShell";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Standard React Error Boundary to catch UI crashes and show a fallback.
 */
export class ErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    error: null,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    // In a production app, we'd log this to an error reporting service
    console.error("Uncaught error:", error, errorInfo);
  }

  private handleReload = () => {
    window.location.reload();
  };

  public render() {
    if (this.state.hasError) {
      return (
        <PageShell>
          <div style={{
            padding: "40px",
            textAlign: "center",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: "20px",
            height: "100%",
            minHeight: "400px",
            justifyContent: "center"
          }}>
            <h1 style={{ color: "var(--danger)" }}>Application Error</h1>
            <p style={{ maxWidth: "500px", color: "var(--text-secondary)" }}>
              The application encountered an unexpected error and cannot continue.
            </p>
            {this.state.error && (
              <pre style={{
                background: "var(--bg-danger)",
                color: "var(--text-danger)",
                padding: "15px",
                borderRadius: "8px",
                fontSize: "12px",
                maxWidth: "600px",
                overflow: "auto",
                border: "1px solid var(--border-danger)"
              }}>
                {this.state.error.message}
              </pre>
            )}
            <button
              type="button"
              onClick={this.handleReload}
              style={{
                background: "var(--primary)",
                color: "white",
                border: "none",
                padding: "10px 24px",
                borderRadius: "20px",
                fontWeight: 600,
                cursor: "pointer"
              }}
            >
              Reload Application
            </button>
          </div>
        </PageShell>
      );
    }

    return this.props.children;
  }
}
