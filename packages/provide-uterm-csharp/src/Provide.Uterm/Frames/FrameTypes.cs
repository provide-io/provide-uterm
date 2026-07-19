//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Frames;

/// <summary>Wire "type" discriminator literals.</summary>
public static class FrameTypeNames
{
    public const string Term = "term";
    public const string Input = "input";
    public const string SnapshotReq = "snapshot_req";
    public const string Snapshot = "snapshot";
    public const string Control = "control";
    public const string HijackState = "hijack_state";
    public const string HijackRequest = "hijack_request";
    public const string HijackRelease = "hijack_release";
    public const string HijackStep = "hijack_step";
    public const string WorkerConnected = "worker_connected";
    public const string WorkerDisconnected = "worker_disconnected";
    public const string WorkerHello = "worker_hello";
    public const string Heartbeat = "heartbeat";
    public const string HeartbeatAck = "heartbeat_ack";
    public const string Ping = "ping";
    public const string Pong = "pong";
    public const string Hello = "hello";
    public const string Resume = "resume";
    public const string Identity = "identity";
    public const string SessionToken = "session_token";
    public const string ResumeOk = "resume_ok";
    public const string ResumeFailed = "resume_failed";
    public const string LinkPatterns = "link_patterns";
    public const string Analysis = "analysis";
    public const string Error = "error";
    public const string Status = "status";
    public const string InputModeChanged = "input_mode_changed";
    public const string ApprovalPending = "approval_pending";
    public const string ApprovalResolved = "approval_resolved";
    public const string PresenceUpdate = "presence_update";
    public const string PresenceSync = "presence_sync";
    public const string PresenceLeave = "presence_leave";
    public const string ControlTransfer = "control_transfer";

    public const int IdentityDefaultVersion = 1;
    public const string IdentityDefaultFingerprint = "";
    public const string IdentityDefaultTransport = "ssh";
}

/// <summary>Frame interface: FrameType returns the wire literal for the struct.</summary>
public interface IFrame
{
    string FrameType { get; }
    string Type { get; set; }
}

public sealed class TermFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Term;
    public string Data { get; set; } = "";
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.Term;
}

public sealed class InputFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Input;
    public string Data { get; set; } = "";
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.Input;
}

public sealed class SnapshotReqFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.SnapshotReq;
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.SnapshotReq;
}

public sealed class SnapshotFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Snapshot;
    public string Screen { get; set; } = "";
    public Dictionary<string, int>? Cursor { get; set; }
    public int? Cols { get; set; }
    public int? Rows { get; set; }
    public string? ScreenHash { get; set; }
    public bool? CursorAtEnd { get; set; }
    public bool? HasTrailingSpace { get; set; }
    public Dictionary<string, object?>? PromptDetected { get; set; }
    public string? RawTail { get; set; }
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.Snapshot;
}

public sealed class ControlFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Control;
    public string Action { get; set; } = "";
    public string? Owner { get; set; }
    public double? LeaseS { get; set; }
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.Control;
}

public sealed class HijackStateFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.HijackState;
    public bool Hijacked { get; set; }
    public string? Owner { get; set; }
    public double? LeaseExpiresAt { get; set; }
    public string? InputMode { get; set; }
    public string FrameType => FrameTypeNames.HijackState;
}

public sealed class HijackRequestFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.HijackRequest;
    public string? Token { get; set; }
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.HijackRequest;
}

public sealed class HijackReleaseFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.HijackRelease;
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.HijackRelease;
}

public sealed class HijackStepFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.HijackStep;
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.HijackStep;
}

public sealed class WorkerConnectedFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.WorkerConnected;
    public string WorkerId { get; set; } = "";
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.WorkerConnected;
}

public sealed class WorkerDisconnectedFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.WorkerDisconnected;
    public string WorkerId { get; set; } = "";
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.WorkerDisconnected;
}

public sealed class WorkerHelloFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.WorkerHello;
    public string? Mode { get; set; }
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.WorkerHello;
}

public sealed class HeartbeatFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Heartbeat;
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.Heartbeat;
}

public sealed class HeartbeatAckFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.HeartbeatAck;
    public double LeaseExpiresAt { get; set; }
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.HeartbeatAck;
}

public sealed class PingFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Ping;
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.Ping;
}

public sealed class PongFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Pong;
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.Pong;
}

public sealed class HelloFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Hello;
    public string? WorkerId { get; set; }
    public bool? CanHijack { get; set; }
    public bool? Hijacked { get; set; }
    public bool? HijackedByMe { get; set; }
    public bool? WorkerOnline { get; set; }
    public string? InputMode { get; set; }
    public string? Role { get; set; }
    public string? HijackControl { get; set; }
    public bool? HijackStepSupported { get; set; }
    public Dictionary<string, object?>? Capabilities { get; set; }
    public bool? ResumeSupported { get; set; }
    public bool? McpSupported { get; set; }
    public bool? VncSupported { get; set; }
    public string? ResumeToken { get; set; }
    public bool? Resumed { get; set; }
    public int? ProtocolVersion { get; set; }
    public Dictionary<string, int>? Protocol { get; set; }
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.Hello;
}

public sealed class ResumeFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Resume;
    public string Token { get; set; } = "";
    public int? PlayerId { get; set; }
    public string FrameType => FrameTypeNames.Resume;
}

public sealed class IdentityFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Identity;
    public int Version { get; set; } = FrameTypeNames.IdentityDefaultVersion;
    public string Subject { get; set; } = "";
    public string Fingerprint { get; set; } = FrameTypeNames.IdentityDefaultFingerprint;
    public string Transport { get; set; } = FrameTypeNames.IdentityDefaultTransport;
    public Dictionary<string, object?>? Claims { get; set; }
    public string? Signature { get; set; }
    public Dictionary<string, object?>? Extra { get; set; }
    public string FrameType => FrameTypeNames.Identity;
}

public sealed class SessionTokenFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.SessionToken;
    public string Token { get; set; } = "";
    public int? PlayerId { get; set; }
    public string FrameType => FrameTypeNames.SessionToken;
}

public sealed class ResumeOkFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.ResumeOk;
    public string FrameType => FrameTypeNames.ResumeOk;
}

public sealed class ResumeFailedFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.ResumeFailed;
    public string? Reason { get; set; }
    public string FrameType => FrameTypeNames.ResumeFailed;
}

public sealed class LinkPatternEntry
{
    public string Pattern { get; set; } = "";
    public string Action { get; set; } = "";
    public string? Id { get; set; }
    public string? Flags { get; set; }
    public object? Group { get; set; }
    public object? Payload { get; set; }
    public string? Hover { get; set; }
    public string? LineContains { get; set; }
    public string? Class { get; set; }
}

public sealed class LinkPatternsFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.LinkPatterns;
    public List<LinkPatternEntry> Patterns { get; set; } = new();
    public string FrameType => FrameTypeNames.LinkPatterns;
}

public sealed class AnalysisFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Analysis;
    public string Formatted { get; set; } = "";
    public object? Raw { get; set; }
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.Analysis;
}

public sealed class ErrorFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Error;
    public string Message { get; set; } = "";
    public string? Reason { get; set; }
    public int? ClientMin { get; set; }
    public int? ClientMax { get; set; }
    public int? ServerMin { get; set; }
    public int? ServerMax { get; set; }
    public string FrameType => FrameTypeNames.Error;
}

public sealed class StatusFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.Status;
    public double? Ts { get; set; }
    public Dictionary<string, object?>? Extra { get; set; }
    public string FrameType => FrameTypeNames.Status;
}

public sealed class InputModeChangedFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.InputModeChanged;
    public string InputMode { get; set; } = "";
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.InputModeChanged;
}

public sealed class ApprovalPendingFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.ApprovalPending;
    public string Command { get; set; } = "";
    public string RequestId { get; set; } = "";
    public double ExpiresAt { get; set; }
    public string FrameType => FrameTypeNames.ApprovalPending;
}

public sealed class ApprovalResolvedFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.ApprovalResolved;
    public string Outcome { get; set; } = "";
    public string RequestId { get; set; } = "";
    public string FrameType => FrameTypeNames.ApprovalResolved;
}

public sealed class PresenceUpdateFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.PresenceUpdate;
    public string? UserId { get; set; }
    public Dictionary<string, object?>? Extra { get; set; }
    public string FrameType => FrameTypeNames.PresenceUpdate;
}

public sealed class PresenceSyncFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.PresenceSync;
    public List<Dictionary<string, object?>>? Users { get; set; }
    public Dictionary<string, object?>? Config { get; set; }
    public string? OwnerId { get; set; }
    public Dictionary<string, object?>? Extra { get; set; }
    public string FrameType => FrameTypeNames.PresenceSync;
}

public sealed class PresenceLeaveFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.PresenceLeave;
    public string UserId { get; set; } = "";
    public double? Ts { get; set; }
    public string FrameType => FrameTypeNames.PresenceLeave;
}

public sealed class ControlTransferFrame : IFrame
{
    public string Type { get; set; } = FrameTypeNames.ControlTransfer;
    public string? FromUserId { get; set; }
    public string? ToUserId { get; set; }
    public string? Reason { get; set; }
    public string? QueuedKeys { get; set; }
    public string FrameType => FrameTypeNames.ControlTransfer;
}
