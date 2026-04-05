//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { widgetSurface } from "../api.js";
export function mountHijackWidget(container, sessionId, surface) {
    const HijackWidget = window.ProvideHijack;
    if (typeof HijackWidget !== "function") {
        return { mounted: false, error: "ProvideHijack is not available" };
    }
    const widgetConfig = widgetSurface(surface);
    new HijackWidget(container, {
        workerId: sessionId,
        showAnalysis: widgetConfig.showAnalysis,
        mobileKeys: widgetConfig.mobileKeys,
    });
    return { mounted: true, error: null };
}
