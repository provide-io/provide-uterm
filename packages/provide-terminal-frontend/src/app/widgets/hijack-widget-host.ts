//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { getShareToken } from "../../server-common.js";
import { widgetSurface } from "../api.js";
import type { SessionSurface, WidgetMountState } from "../types.js";

declare global {
  interface Window {
    ProvideHijack?: new (
      container: HTMLElement,
      config: { workerId: string; showAnalysis?: boolean; mobileKeys?: boolean; authToken?: string },
    ) => unknown;
  }
}

export function mountHijackWidget(
  container: HTMLElement,
  sessionId: string,
  surface: SessionSurface | undefined,
): WidgetMountState {
  const HijackWidget = window.ProvideHijack;
  if (typeof HijackWidget !== "function") {
    return { mounted: false, error: "ProvideHijack is not available" };
  }
  const widgetConfig = widgetSurface(surface);
  const shareToken = getShareToken();
  const config: { workerId: string; showAnalysis?: boolean; mobileKeys?: boolean; authToken?: string } = {
    workerId: sessionId,
    showAnalysis: widgetConfig.showAnalysis,
    mobileKeys: widgetConfig.mobileKeys,
  };
  if (shareToken) {
    config.authToken = shareToken;
  }
  new HijackWidget(container, config);
  return { mounted: true, error: null };
}
