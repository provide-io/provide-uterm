//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The HTTP/JSON surface shared by the FastAPI and Cloudflare backends.
 *
 * Runtime-native operational endpoints deliberately do not appear here;
 * adapters use this inventory as their common contract.
 */

import { type RouteDef, RouteRegistry } from "./registry.ts";

/** Every shared API operation, in declaration order. */
export const API_ROUTES: readonly RouteDef[] = [
  {
    operation: "sessions.list",
    method: "GET",
    template: "/api/sessions",
    scope: "global",
    capability: "sessions.list",
    roles: [],
  },
  {
    operation: "sessions.create",
    method: "POST",
    template: "/api/sessions",
    scope: "global",
    capability: "sessions.create",
    roles: [],
  },
  {
    operation: "sessions.bulk_delete",
    method: "DELETE",
    template: "/api/sessions",
    scope: "global",
    capability: "sessions.bulk_delete",
    roles: ["admin"],
  },
  {
    operation: "sessions.get",
    method: "GET",
    template: "/api/sessions/{session_id}",
    scope: "session",
    capability: "sessions.get",
    roles: [],
  },
  {
    operation: "sessions.update",
    method: "PATCH",
    template: "/api/sessions/{session_id}",
    scope: "session",
    capability: "sessions.update",
    roles: [],
  },
  {
    operation: "sessions.delete",
    method: "DELETE",
    template: "/api/sessions/{session_id}",
    scope: "session",
    capability: "sessions.delete",
    roles: [],
  },
  {
    operation: "sessions.connect",
    method: "POST",
    template: "/api/sessions/{session_id}/connect",
    scope: "session",
    capability: "sessions.connect",
    roles: [],
  },
  {
    operation: "sessions.disconnect",
    method: "POST",
    template: "/api/sessions/{session_id}/disconnect",
    scope: "session",
    capability: "sessions.disconnect",
    roles: [],
  },
  {
    operation: "sessions.restart",
    method: "POST",
    template: "/api/sessions/{session_id}/restart",
    scope: "session",
    capability: "sessions.restart",
    roles: [],
  },
  {
    operation: "sessions.set_mode",
    method: "POST",
    template: "/api/sessions/{session_id}/mode",
    scope: "session",
    capability: "sessions.set_mode",
    roles: [],
  },
  {
    operation: "sessions.clear",
    method: "POST",
    template: "/api/sessions/{session_id}/clear",
    scope: "session",
    capability: "sessions.clear",
    roles: [],
  },
  {
    operation: "sessions.annotate",
    method: "POST",
    template: "/api/sessions/{session_id}/annotate",
    scope: "session",
    capability: "sessions.annotate",
    roles: [],
  },
  {
    operation: "sessions.analyze",
    method: "POST",
    template: "/api/sessions/{session_id}/analyze",
    scope: "session",
    capability: "sessions.analyze",
    roles: [],
  },
  {
    operation: "sessions.snapshot",
    method: "GET",
    template: "/api/sessions/{session_id}/snapshot",
    scope: "session",
    capability: "sessions.snapshot",
    roles: [],
  },
  {
    operation: "sessions.events",
    method: "GET",
    template: "/api/sessions/{session_id}/events",
    scope: "session",
    capability: "sessions.events",
    roles: [],
  },
  {
    operation: "sessions.events_watch",
    method: "GET",
    template: "/api/sessions/{session_id}/events/watch",
    scope: "session",
    capability: "sessions.events_watch",
    roles: [],
  },
  {
    operation: "sessions.events_stream",
    method: "GET",
    template: "/api/sessions/{session_id}/events/stream",
    scope: "session",
    capability: "sessions.events_stream",
    roles: [],
  },
  {
    operation: "sessions.recording",
    method: "GET",
    template: "/api/sessions/{session_id}/recording",
    scope: "session",
    capability: "sessions.recording",
    roles: [],
  },
  {
    operation: "sessions.recording_entries",
    method: "GET",
    template: "/api/sessions/{session_id}/recording/entries",
    scope: "session",
    capability: "sessions.recording_entries",
    roles: [],
  },
  {
    operation: "sessions.recording_download",
    method: "GET",
    template: "/api/sessions/{session_id}/recording/download",
    scope: "session",
    capability: "sessions.recording_download",
    roles: [],
  },
  {
    operation: "sessions.webhooks.create",
    method: "POST",
    template: "/api/sessions/{session_id}/webhooks",
    scope: "session",
    capability: "sessions.webhooks.create",
    roles: [],
  },
  {
    operation: "sessions.webhooks.list",
    method: "GET",
    template: "/api/sessions/{session_id}/webhooks",
    scope: "session",
    capability: "sessions.webhooks.list",
    roles: [],
  },
  {
    operation: "sessions.webhooks.delete",
    method: "DELETE",
    template: "/api/sessions/{session_id}/webhooks/{webhook_id}",
    scope: "session",
    capability: "sessions.webhooks.delete",
    roles: [],
  },
  {
    operation: "tunnels.connect",
    method: "POST",
    template: "/api/connect",
    scope: "global",
    capability: "tunnels.connect",
    roles: [],
  },
  {
    operation: "tunnels.create",
    method: "POST",
    template: "/api/tunnels",
    scope: "global",
    capability: "tunnels.create",
    roles: [],
  },
  {
    operation: "tunnels.revoke_token",
    method: "DELETE",
    template: "/api/tunnels/{tunnel_id}/tokens",
    scope: "global",
    capability: "tunnels.revoke_token",
    roles: [],
  },
  {
    operation: "tunnels.rotate_token",
    method: "POST",
    template: "/api/tunnels/{tunnel_id}/tokens/rotate",
    scope: "global",
    capability: "tunnels.rotate_token",
    roles: [],
  },
  {
    operation: "pam_events.ingest",
    method: "POST",
    template: "/api/pam-events",
    scope: "global",
    capability: "pam_events.ingest",
    roles: ["operator", "admin"],
  },
  {
    operation: "profiles.list",
    method: "GET",
    template: "/api/profiles",
    scope: "global",
    capability: "profiles.list",
    roles: [],
  },
  {
    operation: "profiles.create",
    method: "POST",
    template: "/api/profiles",
    scope: "global",
    capability: "profiles.create",
    roles: [],
  },
  {
    operation: "profiles.get",
    method: "GET",
    template: "/api/profiles/{profile_id}",
    scope: "global",
    capability: "profiles.get",
    roles: [],
  },
  {
    operation: "profiles.update",
    method: "PUT",
    template: "/api/profiles/{profile_id}",
    scope: "global",
    capability: "profiles.update",
    roles: [],
  },
  {
    operation: "profiles.delete",
    method: "DELETE",
    template: "/api/profiles/{profile_id}",
    scope: "global",
    capability: "profiles.delete",
    roles: [],
  },
  {
    operation: "profiles.connect",
    method: "POST",
    template: "/api/profiles/{profile_id}/connect",
    scope: "global",
    capability: "profiles.connect",
    roles: [],
  },
];

/**
 * The validated table.
 *
 * Built at module load, so a malformed or shadowing route fails the import
 * rather than the request that would have hit it.
 */
export const API_ROUTE_REGISTRY = new RouteRegistry(API_ROUTES);
