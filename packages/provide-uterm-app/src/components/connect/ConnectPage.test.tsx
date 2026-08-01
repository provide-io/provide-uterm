//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppBootstrap } from "../../api/types";
import { quickConnect } from "../../api/sessions";
import { ConnectForm } from "./ConnectForm";
import { ConnectPage, saveRecent } from "./ConnectPage";
import { PresetCard } from "./PresetCard";

vi.mock("../../api/sessions", () => ({ quickConnect: vi.fn() }));

const bootstrap: AppBootstrap = {
  page_kind: "connect",
  title: "Connect",
  app_path: "/app",
  assets_path: "/assets",
};

beforeEach(() => localStorage.clear());
afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("connect workflow", () => {
  it("rejects a host transport without a host before calling the API", async () => {
    render(<ConnectForm bootstrap={bootstrap} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByText("Host is required for TELNET connections.")).toBeInTheDocument();
    expect(quickConnect).not.toHaveBeenCalled();
  });

  it("adapts fields when switching between SSH and local shell", () => {
    render(<ConnectForm bootstrap={bootstrap} />);
    const transport = screen.getByRole("combobox");
    const host = screen.getByPlaceholderText("bbs.example.com");
    const port = screen.getByPlaceholderText("23");

    fireEvent.change(transport, { target: { value: "ssh" } });
    expect(port).toHaveValue("22");
    expect(screen.getByPlaceholderText("username")).toBeEnabled();

    fireEvent.change(transport, { target: { value: "shell" } });
    expect(host).toBeDisabled();
    expect(host).toHaveValue("(local)");
  });

  it("surfaces connection failures and re-enables submission", async () => {
    vi.mocked(quickConnect).mockRejectedValue(new Error("gateway unavailable"));
    render(<ConnectForm bootstrap={bootstrap} />);
    fireEvent.change(screen.getByPlaceholderText("bbs.example.com"), { target: { value: "bbs.example" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByText("gateway unavailable")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect" })).toBeEnabled());
    expect(quickConnect).toHaveBeenCalledWith(
      expect.objectContaining({ connector_type: "telnet", host: "bbs.example", port: 23, recording_enabled: true }),
    );
  });

  it("builds a complete SSH request while submission is pending", async () => {
    vi.mocked(quickConnect).mockReturnValue(new Promise(() => {}));
    render(<ConnectForm bootstrap={bootstrap} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "ssh" } });
    fireEvent.change(screen.getByPlaceholderText("bbs.example.com"), { target: { value: "host.test" } });
    fireEvent.change(screen.getByPlaceholderText("23"), { target: { value: "invalid" } });
    fireEvent.change(screen.getByPlaceholderText("username"), { target: { value: " alice " } });
    fireEvent.change(screen.getByPlaceholderText("password"), { target: { value: "secret" } });
    const checks = screen.getAllByRole("checkbox");
    fireEvent.click(checks[0] as HTMLInputElement);
    fireEvent.click(checks[1] as HTMLInputElement);
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("button", { name: "Connecting…" })).toBeDisabled();
    expect(quickConnect).toHaveBeenCalledWith({
      connector_type: "ssh", host: "host.test", port: 22, username: "alice", password: "secret", auto_start: true, // pragma: allowlist secret
    });
  });

  it("builds a local shell request without host fields", async () => {
    vi.mocked(quickConnect).mockReturnValue(new Promise(() => {}));
    render(<ConnectForm bootstrap={bootstrap} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "shell" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByRole("button", { name: "Connecting…" })).toBeDisabled();
    expect(quickConnect).toHaveBeenCalledWith({ connector_type: "shell", recording_enabled: true });
  });

  it("deduplicates recent endpoints, caps storage, and renders the newest three", () => {
    for (let i = 0; i < 8; i += 1) {
      saveRecent({ host: `host-${i}`, transport: "ssh", port: 22, lastUsed: new Date().toISOString() });
    }
    saveRecent({ host: "host-7", transport: "ssh", port: 22, lastUsed: new Date().toISOString() });

    const stored = JSON.parse(localStorage.getItem("uterm-recent-connections") ?? "[]") as unknown[];
    expect(stored).toHaveLength(6);
    render(<ConnectPage bootstrap={bootstrap} />);
    expect(screen.getByText("host-7")).toBeInTheDocument();
    expect(screen.getByText("host-6")).toBeInTheDocument();
    expect(screen.getByText("host-5")).toBeInTheDocument();
    expect(screen.queryByText("host-4")).not.toBeInTheDocument();
  });

  it("ignores malformed recent-connection storage", () => {
    localStorage.setItem("uterm-recent-connections", "not-json");
    render(<ConnectPage bootstrap={bootstrap} />);
    expect(screen.queryByText("Recent connections")).not.toBeInTheDocument();
    expect(screen.getByText("New connection")).toBeInTheDocument();
  });

  it("renders recent timestamps across minute, hour, and day ranges", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-03T12:00:00Z"));
    const { rerender } = render(
      <PresetCard connection={{ host: "now", transport: "ssh", port: 22, lastUsed: "2026-01-03T11:30:00Z" }} />,
    );
    expect(screen.getByText(/just now/)).toBeInTheDocument();
    rerender(<PresetCard connection={{ host: "hours", transport: "ssh", port: 22, lastUsed: "2026-01-03T09:00:00Z" }} />);
    expect(screen.getByText(/3h ago/)).toBeInTheDocument();
    rerender(<PresetCard connection={{ host: "days", transport: "ssh", port: 22, lastUsed: "2026-01-01T12:00:00Z" }} />);
    expect(screen.getByText(/2d ago/)).toBeInTheDocument();
    vi.useRealTimers();
  });
});
