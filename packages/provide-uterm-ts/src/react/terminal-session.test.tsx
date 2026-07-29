//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// The React bindings are the only browser-facing part of the package, so only
// they pay for a DOM.
// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { encodeControlFrame, encodeTerminalData } from "../control-channel/index.ts";
import { type Session, type SessionSocket, statusLabel, TerminalSession, useSession } from "./index.ts";

afterEach(cleanup);

/** A socket a test drives by hand. */
function fakeSocket(): SessionSocket & {
  sent: string[];
  open(): void;
  deliver(message: string): void;
  hangUp(): void;
  closed: boolean;
  listening: boolean;
} {
  const sent: string[] = [];
  let handlers: Parameters<SessionSocket["listen"]>[0] | undefined;
  const socket = {
    sent,
    closed: false,
    get listening() {
      return handlers !== undefined;
    },
    send: (message: string) => {
      sent.push(message);
    },
    close: () => {
      socket.closed = true;
    },
    listen: (given: Parameters<SessionSocket["listen"]>[0]) => {
      handlers = given;
      return () => {
        handlers = undefined;
      };
    },
    open: () => {
      act(() => handlers?.onOpen());
    },
    deliver: (message: string) => {
      act(() => handlers?.onMessage(message));
    },
    hangUp: () => {
      act(() => handlers?.onClose());
    },
  };
  return socket;
}

/**
 * Renders the hook and hands the session to a test.
 *
 * Returned as a function rather than a property: a property read once at
 * destructuring would freeze the session as it was before anything happened,
 * and every assertion after would be about the past.
 */
function renderSession(socket: SessionSocket, viewerId?: string) {
  let session: Session | undefined;
  function Harness(): null {
    session = useSession({ connect: () => socket, ...(viewerId === undefined ? {} : { viewerId }) });
    return null;
  }
  const view = render(<Harness />);
  return { view, session: () => session as Session };
}

describe("driving a session from a socket", () => {
  it("listens as soon as it is mounted, and stops when it is not", () => {
    const socket = fakeSocket();
    const { view } = renderSession(socket);
    expect(socket.listening).toBe(true);
    view.unmount();
    expect(socket.listening).toBe(false);
    expect(socket.closed).toBe(true);
  });

  it("follows the connection opening and closing", () => {
    const socket = fakeSocket();
    const { session } = renderSession(socket);
    expect(session().state.status).toBe("connecting");
    socket.open();
    expect(session().state.status).toBe("open");
    socket.hangUp();
    expect(session().state.status).toBe("closed");
  });

  it("shows terminal bytes as they arrive", () => {
    const socket = fakeSocket();
    const { session } = renderSession(socket);
    socket.open();
    socket.deliver(encodeTerminalData("hello "));
    socket.deliver(encodeTerminalData("world"));
    expect(session().state.screen).toBe("hello world");
  });

  it("applies a control frame", () => {
    const socket = fakeSocket();
    const { session } = renderSession(socket, "ada");
    socket.open();
    socket.deliver(encodeControlFrame({ type: "hijack_state", holder: "ada" }));
    expect(session().state.isHolder).toBe(true);
  });

  it("keeps one decoder for the whole connection", () => {
    // A frame can straddle two messages; decoding each alone would split it.
    const socket = fakeSocket();
    const { session } = renderSession(socket);
    socket.open();
    const frame = encodeControlFrame({ type: "hello", session_id: "sess-1" });
    socket.deliver(frame.slice(0, 6));
    expect(session().state.sessionId).toBeUndefined();
    socket.deliver(frame.slice(6));
    expect(session().state.sessionId).toBe("sess-1");
  });

  it("sends what somebody types, framed", () => {
    const socket = fakeSocket();
    const { session } = renderSession(socket);
    socket.open();
    act(() => session().sendInput("ls\n"));
    expect(socket.sent).toEqual([encodeTerminalData("ls\n")]);
  });

  it("escapes a keystroke that would otherwise read as a control frame", () => {
    // A DLE in what somebody types is doubled on the way out. Sent raw, the
    // far end would take the rest of the line for a control payload — which is
    // somebody typing their way into the control channel.
    const socket = fakeSocket();
    const { session } = renderSession(socket);
    socket.open();
    act(() => session().sendInput("\u0010\u0002{}"));
    expect(socket.sent).toEqual([encodeTerminalData("\u0010\u0002{}")]);
    expect(socket.sent[0]).toBe("\u0010\u0010\u0002{}");
  });

  it("does not send what a viewer may not type", () => {
    // Refused here as well as by the server, so nobody watches their own
    // keystrokes travel and vanish.
    const socket = fakeSocket();
    const { session } = renderSession(socket, "bob");
    socket.open();
    socket.deliver(encodeControlFrame({ type: "hijack_state", holder: "ada" }));
    act(() => session().sendInput("rm -rf /\n"));
    expect(socket.sent).toEqual([]);
  });

  it("does not send an empty keystroke", () => {
    const socket = fakeSocket();
    const { session } = renderSession(socket);
    socket.open();
    act(() => session().sendInput(""));
    expect(socket.sent).toEqual([]);
  });

  it("asks for and gives back control", () => {
    const socket = fakeSocket();
    const { session } = renderSession(socket);
    socket.open();
    act(() => session().requestHijack());
    act(() => session().releaseHijack());
    expect(socket.sent).toEqual([
      encodeControlFrame({ type: "hijack_request" }),
      encodeControlFrame({ type: "hijack_release" }),
    ]);
  });

  it("answers a request either way", () => {
    const socket = fakeSocket();
    const { session } = renderSession(socket);
    socket.open();
    act(() => session().resolveApproval("a1", true));
    act(() => session().resolveApproval("a2", false));
    expect(socket.sent).toEqual([
      encodeControlFrame({ type: "approval_resolved", approval_id: "a1", approved: true }),
      encodeControlFrame({ type: "approval_resolved", approval_id: "a2", approved: false }),
    ]);
  });

  it("forgets the screen locally without telling the server", () => {
    const socket = fakeSocket();
    const { session } = renderSession(socket);
    socket.open();
    socket.deliver(encodeTerminalData("output"));
    act(() => session().clear());
    expect(session().state.screen).toBe("");
    expect(socket.sent).toEqual([]);
  });

  it("says nothing over a socket it has let go", () => {
    // Sending after unmount would be writing to somebody else's connection.
    const socket = fakeSocket();
    const { view, session } = renderSession(socket);
    socket.open();
    view.unmount();
    act(() => session().sendInput("ls\n"));
    expect(socket.sent).toEqual([]);
  });
});

describe("what a session looks like", () => {
  /** A session view rendered over a socket the test drives. */
  function renderView(viewerId?: string) {
    const socket = fakeSocket();
    function App(): React.JSX.Element {
      const session = useSession({ connect: () => socket, ...(viewerId === undefined ? {} : { viewerId }) });
      return <TerminalSession session={session} title="a shell" />;
    }
    const view = render(<App />);
    return { socket, view };
  }

  it("shows what the terminal printed", () => {
    const { socket } = renderView();
    socket.open();
    socket.deliver(encodeTerminalData("hello from the shell"));
    expect(screen.getByTestId("screen").textContent).toBe("hello from the shell");
  });

  it("says why nothing is moving", () => {
    const { socket } = renderView();
    expect(screen.getByRole("status").textContent).toBe("connecting…");
    socket.open();
    expect(screen.getByRole("status").textContent).toBe("connected");
    socket.hangUp();
    expect(screen.getByRole("status").textContent).toBe("disconnected");
  });

  it("says who has control, including when it is you", () => {
    const { socket } = renderView("ada");
    socket.open();
    socket.deliver(encodeControlFrame({ type: "hijack_state", holder: "bob" }));
    expect(screen.getByRole("status").textContent).toBe("bob has control");
    socket.deliver(encodeControlFrame({ type: "hijack_state", holder: "ada" }));
    expect(screen.getByRole("status").textContent).toBe("you have control");
  });

  it("says when a session is only being watched", () => {
    const { socket } = renderView("bob");
    socket.open();
    socket.deliver(encodeControlFrame({ type: "worker_hello", input_mode: "hijack" }));
    expect(screen.getByRole("status").textContent).toBe("connected — read only");
  });

  it("disables typing rather than hiding it", () => {
    // Somebody watching should see that this is where they would type, and
    // that they cannot right now.
    const { socket } = renderView("bob");
    socket.open();
    const input = screen.getByLabelText("send to terminal") as HTMLInputElement;
    expect(input.disabled).toBe(false);
    socket.deliver(encodeControlFrame({ type: "hijack_state", holder: "ada" }));
    expect((screen.getByLabelText("send to terminal") as HTMLInputElement).disabled).toBe(true);
  });

  it("sends a line when it is entered, and empties the box", () => {
    const { socket } = renderView();
    socket.open();
    const input = screen.getByLabelText("send to terminal") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "echo hi" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(socket.sent).toEqual([encodeTerminalData("echo hi\n")]);
    expect(input.value).toBe("");
  });

  it("sends nothing for any other key", () => {
    const { socket } = renderView();
    socket.open();
    const input = screen.getByLabelText("send to terminal") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "echo hi" } });
    fireEvent.keyDown(input, { key: "a" });
    expect(socket.sent).toEqual([]);
  });

  it("lists who else is watching", () => {
    const { socket } = renderView();
    socket.open();
    socket.deliver(
      encodeControlFrame({
        type: "presence_sync",
        participants: [
          { id: "v1", name: "Ada", role: "operator", color: "#ff0000" },
          { id: "v2", name: "Bob" },
        ],
      }),
    );
    const list = screen.getByLabelText("who is watching");
    expect(list.textContent).toContain("Ada (operator)");
    expect(list.textContent).toContain("Bob (viewer)");
  });

  it("shows nobody watching as nothing at all", () => {
    // An empty list is a heading with nothing under it, which reads as broken.
    const { socket } = renderView();
    socket.open();
    expect(screen.queryByLabelText("who is watching")).toBeNull();
  });

  it("asks about a pending request and answers it", () => {
    const { socket } = renderView();
    socket.open();
    socket.deliver(
      encodeControlFrame({ type: "approval_pending", approval_id: "a1", subject: "Ada", reason: "to fix the build" }),
    );
    const prompt = screen.getByRole("alertdialog");
    expect(prompt.textContent).toContain("Ada is asking to take control: to fix the build");
    fireEvent.click(screen.getByText("Allow"));
    expect(socket.sent).toEqual([encodeControlFrame({ type: "approval_resolved", approval_id: "a1", approved: true })]);
  });

  it("asks without a reason when the server gave none", () => {
    const { socket } = renderView();
    socket.open();
    socket.deliver(encodeControlFrame({ type: "approval_pending", approval_id: "a1", subject: "Ada" }));
    expect(screen.getByRole("alertdialog").textContent).toContain("Ada is asking to take control");
    fireEvent.click(screen.getByText("Refuse"));
    expect(socket.sent).toEqual([
      encodeControlFrame({ type: "approval_resolved", approval_id: "a1", approved: false }),
    ]);
  });

  it("shows an error where somebody will see it", () => {
    const { socket } = renderView();
    socket.open();
    socket.deliver(encodeControlFrame({ type: "error", message: "lease denied" }));
    expect(screen.getByRole("alert").textContent).toBe("lease denied");
  });

  it("shows no alert when nothing has gone wrong", () => {
    const { socket } = renderView();
    socket.open();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers to take control, then to give it back", () => {
    const { socket } = renderView("ada");
    socket.open();
    expect(screen.getByText("Request control")).toBeTruthy();
    fireEvent.click(screen.getByText("Request control"));
    expect(socket.sent).toEqual([encodeControlFrame({ type: "hijack_request" })]);
    socket.deliver(encodeControlFrame({ type: "hijack_state", holder: "ada" }));
    fireEvent.click(screen.getByText("Release control"));
    expect(socket.sent).toEqual([
      encodeControlFrame({ type: "hijack_request" }),
      encodeControlFrame({ type: "hijack_release" }),
    ]);
  });

  it("renders without a title", () => {
    const socket = fakeSocket();
    function App(): React.JSX.Element {
      return <TerminalSession session={useSession({ connect: () => socket })} />;
    }
    render(<App />);
    expect(screen.getByLabelText("terminal session")).toBeTruthy();
    expect(screen.queryByRole("heading")).toBeNull();
  });
});

describe("the one-line status", () => {
  /** The least of a session needed to ask about its label. */
  function sessionWith(state: Partial<Session["state"]>, allowed = true): Session {
    return {
      state: {
        status: "open",
        screen: "",
        sessionId: undefined,
        hijackHolder: undefined,
        isHolder: false,
        inputMode: "open",
        participants: [],
        approvals: [],
        error: undefined,
        reconnects: 0,
        ...state,
      },
      canType: allowed,
      sendInput: () => {},
      requestHijack: () => {},
      releaseHijack: () => {},
      resolveApproval: () => {},
      clear: () => {},
    };
  }

  it("prefers the reason nothing is moving over the reason nothing can be typed", () => {
    // "connecting" tells somebody to wait; "read only" would tell them to give
    // up.
    expect(statusLabel(sessionWith({ status: "connecting" }, false))).toBe("connecting…");
    expect(statusLabel(sessionWith({ status: "closed" }, false))).toBe("disconnected");
  });

  it("names the holder before it names the mode", () => {
    expect(statusLabel(sessionWith({ isHolder: true, hijackHolder: "ada" }))).toBe("you have control");
    expect(statusLabel(sessionWith({ hijackHolder: "ada" }, false))).toBe("ada has control");
  });

  it("says read-only only when nobody in particular holds it", () => {
    expect(statusLabel(sessionWith({}, false))).toBe("connected — read only");
    expect(statusLabel(sessionWith({}, true))).toBe("connected");
  });
});
