//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// AUTO-GENERATED — DO NOT EDIT. Regenerate via scripts/codegen_frames.py.
//
export type AnyFrame =
  | TermFrame
  | InputFrame
  | SnapshotReqFrame
  | SnapshotFrame
  | ControlFrame
  | HijackStateFrame
  | HijackRequestFrame
  | HijackReleaseFrame
  | HijackStepFrame
  | WorkerConnectedFrame
  | WorkerDisconnectedFrame
  | WorkerHelloFrame
  | HeartbeatFrame
  | HeartbeatAckFrame
  | PingFrame
  | PongFrame
  | HelloFrame
  | ResumeFrame
  | IdentityFrame
  | SessionTokenFrame
  | ResumeOkFrame
  | ResumeFailedFrame
  | LinkPatternsFrame
  | AnalysisFrame
  | ErrorFrame
  | StatusFrame
  | InputModeChangedFrame
  | ApprovalPendingFrame
  | ApprovalResolvedFrame
  | PresenceUpdateFrame
  | PresenceSyncFrame
  | PresenceLeaveFrame
  | ControlTransferFrame;
export type Data = string;
export type Ts = number | null;
export type Type = "term";
export type Data1 = string;
export type Ts1 = number | null;
export type Type1 = "input";
export type Ts2 = number | null;
export type Type2 = "snapshot_req";
export type Cols = number | null;
export type Cursor = {
  [k: string]: number;
} | null;
export type CursorAtEnd = boolean | null;
export type HasTrailingSpace = boolean | null;
export type PromptDetected = {
  [k: string]: unknown;
} | null;
export type RawTail = string | null;
export type Rows = number | null;
export type Screen = string;
export type ScreenHash = string | null;
export type Ts3 = number | null;
export type Type3 = "snapshot";
export type Action = string;
export type LeaseS = number | null;
export type Owner = string | null;
export type Ts4 = number | null;
export type Type4 = "control";
export type Hijacked = boolean;
export type InputMode = string | null;
export type LeaseExpiresAt = number | null;
export type Owner1 = string | null;
export type Type5 = "hijack_state";
export type Token = string | null;
export type Ts5 = number | null;
export type Type6 = "hijack_request";
export type Ts6 = number | null;
export type Type7 = "hijack_release";
export type Ts7 = number | null;
export type Type8 = "hijack_step";
export type Ts8 = number | null;
export type Type9 = "worker_connected";
export type WorkerId = string;
export type Ts9 = number | null;
export type Type10 = "worker_disconnected";
export type WorkerId1 = string;
export type Mode = string | null;
export type Ts10 = number | null;
export type Type11 = "worker_hello";
export type Ts11 = number | null;
export type Type12 = "heartbeat";
export type LeaseExpiresAt1 = number;
export type Ts12 = number | null;
export type Type13 = "heartbeat_ack";
export type Ts13 = number | null;
export type Type14 = "ping";
export type Ts14 = number | null;
export type Type15 = "pong";
export type CanHijack = boolean | null;
export type Capabilities = {
  [k: string]: unknown;
} | null;
export type HijackControl = string | null;
export type HijackStepSupported = boolean | null;
export type Hijacked1 = boolean | null;
export type HijackedByMe = boolean | null;
export type InputMode1 = string | null;
export type Protocol = {
  [k: string]: number;
} | null;
export type ProtocolVersion = number | null;
export type ResumeSupported = boolean | null;
export type ResumeToken = string | null;
export type Resumed = boolean | null;
export type Role = string | null;
export type Ts15 = number | null;
export type Type16 = "hello";
export type WorkerId2 = string | null;
export type WorkerOnline = boolean | null;
export type PlayerId = number | null;
export type Token1 = string;
export type Type17 = "resume";
export type Claims = {
  [k: string]: unknown;
} | null;
export type Fingerprint = string;
export type Signature = string | null;
export type Subject = string;
export type Transport = string;
export type Type18 = "identity";
export type Version = number;
export type PlayerId1 = number | null;
export type Token2 = string;
export type Type19 = "session_token";
export type Type20 = "resume_ok";
export type Reason = string | null;
export type Type21 = "resume_failed";
export type Action1 = "cmd" | "url" | "key" | "focus";
export type Class = string | null;
export type Flags = string | null;
export type Group = number | string | null;
export type Hover = string | null;
export type Id = string | null;
export type LineContains = string | null;
export type Pattern = string;
export type Patterns = LinkPatternEntry[];
export type Type22 = "link_patterns";
export type Formatted = string;
export type Ts16 = number | null;
export type Type23 = "analysis";
export type ClientMax = number | null;
export type ClientMin = number | null;
export type Message = string;
export type Reason1 = string | null;
export type ServerMax = number | null;
export type ServerMin = number | null;
export type Type24 = "error";
export type Ts17 = number | null;
export type Type25 = "status";
export type InputMode2 = string;
export type Ts18 = number | null;
export type Type26 = "input_mode_changed";
export type Command = string;
export type ExpiresAt = number;
export type RequestId = string;
export type Type27 = "approval_pending";
export type Outcome = string;
export type RequestId1 = string;
export type Type28 = "approval_resolved";
export type Type29 = "presence_update";
export type UserId = string | null;
export type Config = {
  [k: string]: unknown;
} | null;
export type OwnerId = string | null;
export type Type30 = "presence_sync";
export type Users =
  | {
      [k: string]: unknown;
    }[]
  | null;
export type Ts19 = number | null;
export type Type31 = "presence_leave";
export type UserId1 = string;
export type FromUserId = string | null;
export type QueuedKeys = string | null;
export type Reason2 = string | null;
export type ToUserId = string | null;
export type Type32 = "control_transfer";

/**
 * Raw terminal output bytes from the worker to subscribers.
 */
export interface TermFrame {
  data: Data;
  ts?: Ts;
  type: Type;
}
/**
 * Browser/operator input destined for the worker.
 */
export interface InputFrame {
  data: Data1;
  ts?: Ts1;
  type: Type1;
}
/**
 * Browser-originated request for a fresh screen snapshot.
 */
export interface SnapshotReqFrame {
  ts?: Ts2;
  type: Type2;
}
/**
 * Worker-originated full-screen snapshot.
 */
export interface SnapshotFrame {
  cols?: Cols;
  cursor?: Cursor;
  cursor_at_end?: CursorAtEnd;
  has_trailing_space?: HasTrailingSpace;
  prompt_detected?: PromptDetected;
  raw_tail?: RawTail;
  rows?: Rows;
  screen: Screen;
  screen_hash?: ScreenHash;
  ts?: Ts3;
  type: Type3;
}
/**
 * Server-originated worker-control frame (pause/resume/step).
 */
export interface ControlFrame {
  action: Action;
  lease_s?: LeaseS;
  owner?: Owner;
  ts?: Ts4;
  type: Type4;
}
/**
 * Broadcast lease-state update.
 */
export interface HijackStateFrame {
  hijacked: Hijacked;
  input_mode?: InputMode;
  lease_expires_at?: LeaseExpiresAt;
  owner?: Owner1;
  type: Type5;
}
/**
 * Browser-originated request to acquire the hijack lease.
 */
export interface HijackRequestFrame {
  token?: Token;
  ts?: Ts5;
  type: Type6;
}
/**
 * Browser-originated request to release the hijack lease.
 */
export interface HijackReleaseFrame {
  ts?: Ts6;
  type: Type7;
}
/**
 * Browser-originated single-step request.
 */
export interface HijackStepFrame {
  ts?: Ts7;
  type: Type8;
}
export interface WorkerConnectedFrame {
  ts?: Ts8;
  type: Type9;
  worker_id: WorkerId;
}
export interface WorkerDisconnectedFrame {
  ts?: Ts9;
  type: Type10;
  worker_id: WorkerId1;
}
/**
 * Worker-originated hello-frame carrying input_mode + capabilities.
 */
export interface WorkerHelloFrame {
  mode?: Mode;
  ts?: Ts10;
  type: Type11;
}
export interface HeartbeatFrame {
  ts?: Ts11;
  type: Type12;
}
/**
 * Server reply to a browser heartbeat — refreshes the lease.
 */
export interface HeartbeatAckFrame {
  lease_expires_at: LeaseExpiresAt1;
  ts?: Ts12;
  type: Type13;
}
export interface PingFrame {
  ts?: Ts13;
  type: Type14;
}
export interface PongFrame {
  ts?: Ts14;
  type: Type15;
}
/**
 * Server-originated hello-frame to the browser describing capabilities.
 *
 * Schema is intentionally permissive (``extra="ignore"``) because the field
 * set drifts as new capabilities land; field-by-field tightening will happen
 * once the wire format is fully stable.
 */
export interface HelloFrame {
  can_hijack?: CanHijack;
  capabilities?: Capabilities;
  hijack_control?: HijackControl;
  hijack_step_supported?: HijackStepSupported;
  hijacked?: Hijacked1;
  hijacked_by_me?: HijackedByMe;
  input_mode?: InputMode1;
  protocol?: Protocol;
  protocol_version?: ProtocolVersion;
  resume_supported?: ResumeSupported;
  resume_token?: ResumeToken;
  resumed?: Resumed;
  role?: Role;
  ts?: Ts15;
  type: Type16;
  worker_id?: WorkerId2;
  worker_online?: WorkerOnline;
}
export interface ResumeFrame {
  player_id?: PlayerId;
  token: Token1;
  type: Type17;
}
/**
 * Inline control-channel identity frame.
 */
export interface IdentityFrame {
  claims?: Claims;
  fingerprint?: Fingerprint;
  signature?: Signature;
  subject: Subject;
  transport?: Transport;
  type: Type18;
  version?: Version;
  [k: string]: unknown;
}
export interface SessionTokenFrame {
  player_id?: PlayerId1;
  token: Token2;
  type: Type19;
}
export interface ResumeOkFrame {
  type: Type20;
}
export interface ResumeFailedFrame {
  reason?: Reason;
  type: Type21;
}
export interface LinkPatternsFrame {
  patterns: Patterns;
  type: Type22;
}
export interface LinkPatternEntry {
  action: Action1;
  class?: Class;
  flags?: Flags;
  group?: Group;
  hover?: Hover;
  id?: Id;
  line_contains?: LineContains;
  pattern: Pattern;
  payload?: unknown;
}
export interface AnalysisFrame {
  formatted: Formatted;
  raw?: unknown;
  ts?: Ts16;
  type: Type23;
}
export interface ErrorFrame {
  client_max?: ClientMax;
  client_min?: ClientMin;
  message: Message;
  reason?: Reason1;
  server_max?: ServerMax;
  server_min?: ServerMin;
  type: Type24;
}
/**
 * Worker-originated status passthrough (``coerce_worker_status_frame``).
 *
 * Schema is permissive because the worker may attach arbitrary status
 * payloads. The frame type discriminator and ``ts`` field are the only
 * guarantees.
 */
export interface StatusFrame {
  ts?: Ts17;
  type: Type25;
  [k: string]: unknown;
}
export interface InputModeChangedFrame {
  input_mode: InputMode2;
  ts?: Ts18;
  type: Type26;
}
export interface ApprovalPendingFrame {
  command: Command;
  expires_at: ExpiresAt;
  request_id: RequestId;
  type: Type27;
}
export interface ApprovalResolvedFrame {
  outcome: Outcome;
  request_id: RequestId1;
  type: Type28;
}
/**
 * DeckMux per-user presence update — schema is permissive because
 * optional fields (scroll, selection, pin, typing, queued_keys) are
 * attached only when relevant.
 */
export interface PresenceUpdateFrame {
  type: Type29;
  user_id?: UserId;
  [k: string]: unknown;
}
/**
 * Full presence roster sent on browser connect.
 */
export interface PresenceSyncFrame {
  config?: Config;
  owner_id?: OwnerId;
  type: Type30;
  users?: Users;
  [k: string]: unknown;
}
export interface PresenceLeaveFrame {
  ts?: Ts19;
  type: Type31;
  user_id: UserId1;
}
/**
 * DeckMux ownership-transfer notice.
 */
export interface ControlTransferFrame {
  from_user_id?: FromUserId;
  queued_keys?: QueuedKeys;
  reason?: Reason2;
  to_user_id?: ToUserId;
  type: Type32;
}
