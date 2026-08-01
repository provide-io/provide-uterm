//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RecordingEntryView } from "../../api/types";
import { useReplayStore } from "../../stores/replayStore";
import { EventDetail } from "./EventDetail";
import { PlaybackControls } from "./PlaybackControls";
import { ScreenPreview } from "./ScreenPreview";
import { TimelineCanvas } from "./TimelineCanvas";

const entries: RecordingEntryView[] = [
  { ts: 1.25, event: "read", payload: { data: "hello" }, screen: "\u001b[31mred\u001b[0m" },
  { ts: 62, event: "send", payload: { data: "world" }, screen: "plain" },
];

beforeEach(() => {
  useReplayStore.setState({
    entries,
    index: 0,
    filter: "",
    limit: 200,
    loading: false,
    error: null,
    playing: false,
    speed: 1,
    load: vi.fn(),
  });
});

describe("replay workflow", () => {
  it("renders event metadata and an ANSI screen, then switches to raw", () => {
    const { rerender } = render(<EventDetail entry={null} />);
    expect(screen.getByText("No event selected.")).toBeInTheDocument();
    rerender(<EventDetail entry={entries[0] ?? null} />);
    expect(screen.getByText("0:00:01.250")).toBeInTheDocument();
    expect(screen.getByText("hello", { exact: false })).toBeInTheDocument();

    const view = render(<ScreenPreview entry={entries[0] ?? null} index={0} />);
    expect(view.getByText("red")).toHaveStyle({ color: "rgb(204, 0, 0)" });
    fireEvent.click(view.getByRole("button", { name: "Raw" }));
    expect(view.container.querySelector("pre")?.textContent).toContain("\u001b[31mred");
  });

  it("connects playback navigation, speed, filter, and limit controls to the store", () => {
    render(<PlaybackControls sessionId="session-1" />);
    fireEvent.click(screen.getByRole("button", { name: ">" }));
    expect(useReplayStore.getState().index).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "|<" }));
    expect(useReplayStore.getState().index).toBe(0);

    const selects = screen.getAllByRole("combobox");
    fireEvent.change(selects[0] as HTMLSelectElement, { target: { value: "2" } });
    fireEvent.change(selects[1] as HTMLSelectElement, { target: { value: "read" } });
    fireEvent.change(selects[2] as HTMLSelectElement, { target: { value: "25" } });
    expect(useReplayStore.getState()).toMatchObject({ speed: 2, filter: "read", limit: 25 });
    expect(useReplayStore.getState().load).toHaveBeenCalledTimes(2);
  });

  it("draws event density and seeks by click position", () => {
    const ctx = {
      scale: vi.fn(), fillRect: vi.fn(), beginPath: vi.fn(), roundRect: vi.fn(), fill: vi.fn(), fillText: vi.fn(),
      fillStyle: "", globalAlpha: 1, font: "", textAlign: "left",
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, left: 0, top: 0, right: 300, bottom: 48, width: 300, height: 48, toJSON: () => ({}),
    });

    const { container } = render(<TimelineCanvas />);
    expect(ctx.fillRect).toHaveBeenCalled();
    fireEvent.click(container.querySelector("canvas") as HTMLCanvasElement, { clientX: 300 });
    expect(useReplayStore.getState().index).toBe(1);
  });
});
