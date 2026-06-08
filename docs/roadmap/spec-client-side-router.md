# Specification: Client-Side Router for Admin Console

## Overview
The frontend administration application currently uses basic state-based view switching. Moving to a dedicated client-side router (e.g., React Router or TanStack Router) will allow deep-linking, support for browser history (back/forward buttons), and shareable URLs for specific sessions or configurations.

## Requirements
- Introduce a lightweight client-side routing library.
- Refactor existing views (Session List, Terminal View, Settings) to discrete route paths (e.g., `/sessions`, `/sessions/:id`).
- Ensure deep links correctly trigger authentication guards before rendering views.

## Scope
- Refactoring `packages/provide-uterm-app` entry points and view state management.
