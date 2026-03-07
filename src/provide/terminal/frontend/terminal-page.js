"use strict";
function initTerminalPage() {
    const container = document.getElementById("app");
    if (!(container instanceof HTMLElement)) {
        throw new Error("Missing #app container");
    }
    const TerminalWidget = window.ProvideTerminal;
    if (typeof TerminalWidget !== "function") {
        throw new Error("ProvideTerminal is not available");
    }
    window.demoTerminal = new TerminalWidget(container);
}
initTerminalPage();
