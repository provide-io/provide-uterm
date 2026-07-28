//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The records the control plane stores.
 *
 * Port of the Python modules under `provide.uterm.control.plane`.
 */

/** Which backend a deployment is running on. */
export type ControlPlaneBackend = "memory" | "sqlite";

/** What a session is doing. */
export type LifecycleState = "waiting" | "running" | "stopped" | "error" | "deleted";

/** Who may see a session. */
export type Visibility = "public" | "operator" | "private";

/** Where an approval has got to. */
export type ApprovalState = "pending" | "approved" | "rejected";

/** Engine feature flags, discovered at bootstrap. */
export interface EngineCapabilities {
  /** Whether the engine can group writes. */
  supportsTransactions: boolean;
  /** Whether it has a schema to migrate. */
  supportsMigrations: boolean;
  /** Whether a conflicting write is worth retrying. */
  supportsRetries: boolean;
}

/** The capabilities an engine has unless it says otherwise. */
export const DEFAULT_CAPABILITIES: EngineCapabilities = {
  supportsTransactions: true,
  supportsMigrations: true,
  supportsRetries: true,
};

/** How a control plane is set up. */
export interface ControlPlaneConfig {
  /** Which backend to run. */
  backend?: ControlPlaneBackend;
  /** Where the durable one stores. */
  databaseUrl?: string;
  /** What the engine can do. */
  capabilities?: EngineCapabilities;
}

/** The configuration a deployment gets by default. */
export const CONTROL_PLANE_DEFAULTS = {
  backend: "memory" as ControlPlaneBackend,
  databaseUrl: ":memory:",
};

/** A hosted session. */
export interface SessionRecord {
  sessionId: string;
  displayName: string;
  connectorType: string;
  owner?: string | undefined;
  visibility: Visibility;
  lifecycleState: LifecycleState;
  createdAt: number;
  updatedAt: number;
  deletedAt?: number | undefined;
}

/** A hijack lease. */
export interface LeaseRecord {
  sessionId: string;
  hijackId: string;
  owner: string;
  leaseExpiresAt: number;
  createdAt: number;
  deletedAt?: number | undefined;
}

/** A command held for approval. */
export interface ApprovalRecord {
  approvalId: string;
  sessionId: string;
  command: string;
  requestedBy?: string | undefined;
  state: ApprovalState;
  createdAt: number;
  resolvedAt?: number | undefined;
  resolvedBy?: string | undefined;
}

/** A token bound to one session and purpose. */
export interface SessionTokenRecord {
  sessionId: string;
  tokenKind: string;
  tokenValue: string;
  createdAt: number;
  expiresAt?: number | undefined;
  revokedAt?: number | undefined;
}

/** A token that lets a client pick a session back up. */
export interface ResumeTokenRecord {
  tokenValue: string;
  sessionId: string;
  role: string;
  createdAt: number;
  expiresAt: number;
  wasHijackOwner?: boolean;
  revokedAt?: number | undefined;
}

/** A graphical target a session can be attached to. */
export interface GraphicalTargetRecord {
  targetId: string;
  [key: string]: unknown;
}
