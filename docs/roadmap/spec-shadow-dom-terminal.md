# Specification: Web Components / Shadow DOM for Terminal Isolation

## Overview
Currently, the `xterm.js` widget relies on global CSS rules. This poses a risk when the terminal is embedded in external web applications, as host styles can leak into the terminal (breaking layout) and terminal styles can leak into the host.
Wrapping the terminal inside a Shadow DOM provides hard CSS encapsulation.

## Requirements
- Refactor the React `TerminalHost` component to render inside a Shadow Root.
- Inject `xterm.css` and custom uterm theme CSS explicitly inside the Shadow DOM.
- Expose a `provide-terminal` Custom Element (Web Component) wrapper for zero-React integration.

## Scope
- Frontend presentation layer only. Does not impact the data bridge or websocket protocol.
