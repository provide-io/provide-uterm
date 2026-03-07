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
