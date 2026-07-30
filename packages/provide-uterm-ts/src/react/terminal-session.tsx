//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A session, on screen.
 *
 * Deliberately plain: the screen, who else is here, what is waiting for a
 * decision, and whether this viewer may type. Everything it renders comes
 * from {@link useSession}, so what it shows is a function of what the server
 * said and nothing else.
 */

import type { JSX } from "react";
import type { Session } from "./use-session.ts";

/** What to render. */
export interface TerminalSessionProps {
  session: Session;
  /** Shown above the screen. */
  title?: string | undefined;
}

/** One line describing the connection, for somebody wondering why nothing moves. */
export function statusLabel(session: Session): string {
  const { status, hijackHolder, isHolder } = session.state;
  if (status === "connecting") {
    return "connecting…";
  }
  if (status === "closed") {
    return "disconnected";
  }
  if (isHolder) {
    return "you have control";
  }
  if (hijackHolder !== undefined) {
    return `${hijackHolder} has control`;
  }
  return session.canType ? "connected" : "connected — read only";
}

/** A session view. */
export function TerminalSession({ session, title }: TerminalSessionProps): JSX.Element {
  const { state } = session;

  return (
    <section aria-label={title ?? "terminal session"} className="uterm-session">
      <header className="uterm-session__header">
        {title !== undefined ? <h2 className="uterm-session__title">{title}</h2> : null}
        <p className="uterm-session__status" role="status">
          {statusLabel(session)}
        </p>
      </header>

      {state.error !== undefined ? (
        <p className="uterm-session__error" role="alert">
          {state.error}
        </p>
      ) : null}

      {/* role="log" rather than a bare <pre>: a <pre> has no ARIA role, and a
          generic role does not support aria-label — assistive tech dropped the
          label entirely. "log" is also the right semantics for output that is
          appended to over time. */}
      <pre className="uterm-session__screen" role="log" aria-label="terminal output" data-testid="screen">
        {state.screen}
      </pre>

      {state.participants.length > 0 ? (
        <ul className="uterm-session__presence" aria-label="who is watching">
          {state.participants.map((participant) => (
            <li key={participant.id} style={participant.colour !== undefined ? { color: participant.colour } : {}}>
              {participant.name} ({participant.role})
            </li>
          ))}
        </ul>
      ) : null}

      {state.approvals.map((approval) => (
        <div key={approval.id} className="uterm-session__approval" role="alertdialog" aria-label="approval requested">
          <p>
            {approval.subject} is asking to take control
            {approval.reason !== undefined ? `: ${approval.reason}` : ""}
          </p>
          <button type="button" onClick={() => session.resolveApproval(approval.id, true)}>
            Allow
          </button>
          <button type="button" onClick={() => session.resolveApproval(approval.id, false)}>
            Refuse
          </button>
        </div>
      ))}

      <input
        className="uterm-session__input"
        aria-label="send to terminal"
        // Disabled rather than hidden: somebody watching should be able to see
        // that typing is possible here, and why it is not possible now.
        disabled={!session.canType}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            session.sendInput(`${event.currentTarget.value}\n`);
            event.currentTarget.value = "";
          }
        }}
      />

      <button type="button" onClick={state.isHolder ? session.releaseHijack : session.requestHijack}>
        {state.isHolder ? "Release control" : "Request control"}
      </button>
    </section>
  );
}
