//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useTerminalStore } from "../../stores/terminalStore";

vi.mock("@provide-uterm-frontend/terminal-element", () => ({}));
import { TerminalHost } from "./TerminalHost";

const instances: StubTerminal[] = [];
class StubTerminal extends HTMLElement {
  config: Record<string, unknown> | null = null;
  connect = vi.fn();
  constructor() {
    super();
    instances.push(this);
  }
}
if (!customElements.get("uterm-terminal")) customElements.define("uterm-terminal", StubTerminal);

beforeEach(() => {
  instances.length = 0;
  useTerminalStore.setState({ mounted: false, error: null, cols: 0, rows: 0 });
});

describe("TerminalHost", () => {
  it("configures and connects the terminal custom element once", () => {
    const config = { wsUrl: "/ws/raw/session/term", title: "Session" };
    const { rerender } = render(<TerminalHost config={config} />);
    expect(instances[0]?.config).toEqual(config);
    expect(instances[0]?.connect).toHaveBeenCalledOnce();
    expect(useTerminalStore.getState().mounted).toBe(true);

    rerender(<TerminalHost config={config} />);
    expect(instances[0]?.connect).toHaveBeenCalledOnce();
  });

  it("uses an empty config when none is supplied", () => {
    render(<TerminalHost />);
    expect(instances[0]?.config).toEqual({});
  });
});
