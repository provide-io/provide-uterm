//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>How far <see cref="TermHub.ResolveApprovalAsync"/> got.</summary>
/// <remarks>
/// Go returns <c>(resolved bool, err error)</c> and its REST route reads the two
/// together. A tri-state says the same thing once, and keeps the route from
/// having to know that "resolved, with an error" means 409 while "resolved, no
/// error" means 200.
/// </remarks>
public enum ApprovalResolution
{
    /// <summary>Unknown id, or already approved/rejected/timed out. The route answers 400.</summary>
    NotPending,

    /// <summary>Claimed and carried out. The route answers 200.</summary>
    Resolved,

    /// <summary>
    /// Claimed, but the submitter's capability fence had lapsed, so the held
    /// command was never injected. The route answers 409.
    /// </summary>
    Refused,
}

/// <summary>Why <see cref="TermHub.ParkBrowserForApprovalAsync"/> declined to park.</summary>
public static class ApprovalParkReasons
{
    /// <summary>The submitter no longer holds an input capability at the captured generation.</summary>
    public const string OwnershipInvalid = "ownership_invalid";

    /// <summary>A request with the gate-supplied id already exists.</summary>
    public const string DuplicateId = "duplicate_id";
}

/// <summary>
/// The parked request id, or <see cref="Reason"/> saying why nothing was parked.
/// </summary>
public readonly record struct ApprovalParkResult(string? RequestId, string Reason);

// Browser-input approval orchestration: the policy gate, the parked-browser
// hold buffers, and the one-shot resolve that injects or refuses a held
// command. Port of Go hub/approvals_resolve.go.
public sealed partial class TermHub
{
    /// <summary>The configured input policy gate; never null.</summary>
    public IInputPolicyGate PolicyGate { get; }

    /// <summary>Browsers held awaiting an approval decision.</summary>
    private HashSet<object> PausedBrowsers { get; } = new();

    /// <summary>Keystrokes typed by a parked browser, replayed when it is approved.</summary>
    private Dictionary<object, string> HoldBuffers { get; } = new();

    /// <summary>
    /// Whether the configured gate is the default allow-everything one. When it
    /// is, the browser input path forwards directly and never builds a policy
    /// context. Port of Go <c>IsNoOpPolicyGate</c>.
    /// </summary>
    public bool IsNoOpPolicyGate => PolicyGate is NoOpPolicyGate;

    /// <summary>Whether <paramref name="ws"/> is held awaiting an approval decision.</summary>
    public bool IsBrowserParked(object ws)
    {
        lock (SharedLock) return PausedBrowsers.Contains(ws);
    }

    /// <summary>
    /// Append to a parked browser's hold buffer, returning true when the append
    /// would exceed <see cref="MaxBufferChars"/> — in which case nothing is
    /// stored, so the buffer already accumulated survives. Port of Go
    /// <c>HoldBrowserInput</c>.
    /// </summary>
    public bool HoldBrowserInput(object ws, string data)
    {
        lock (SharedLock) return !AppendHoldLocked(ws, data);
    }

    /// <summary>
    /// Check that <paramref name="ws"/> is still parked and buffer
    /// <paramref name="data"/> in the same transition.
    /// </summary>
    /// <remarks>
    /// Held=false tells the caller an approval concurrently unparked this
    /// browser, so normal fenced delivery must continue rather than the
    /// keystroke being dropped between the two checks. Port of Go
    /// <c>TryHoldBrowserInput</c>.
    /// </remarks>
    public (bool Held, bool TooLong) TryHoldBrowserInput(object ws, string data)
    {
        lock (SharedLock)
        {
            if (!PausedBrowsers.Contains(ws)) return (false, false);
            return (true, !AppendHoldLocked(ws, data));
        }
    }

    /// <summary>Append to the hold buffer under <see cref="SharedLock"/>; false when it would overflow.</summary>
    private bool AppendHoldLocked(object ws, string data)
    {
        var held = HoldBuffers.GetValueOrDefault(ws, string.Empty) + data;
        if (held.Length > MaxBufferChars) return false;
        HoldBuffers[ws] = held;
        return true;
    }

    /// <summary>
    /// Build the policy context for a browser. Port of Go
    /// <c>StateStore.PreparePolicyContext</c>, reading the role the hub already
    /// resolved at registration rather than re-resolving it.
    /// </summary>
    public PolicyContext PreparePolicyContext(string workerId, object ws, string? action)
    {
        lock (SharedLock)
        {
            var st = Registry.Get(workerId);
            return new PolicyContext
            {
                WorkerId = workerId,
                ClientId = BrowserPrincipals.GetValueOrDefault(ws) ?? "anonymous",
                Role = st is not null && st.Browsers.TryGetValue(ws, out var role) ? role : null,
                Action = action,
            };
        }
    }

    /// <summary>Run the input policy gate for one browser input frame. Port of Go <c>InterceptBrowserInput</c>.</summary>
    public Task<PolicyDecision> InterceptBrowserInputAsync(
        string workerId,
        object ws,
        string data,
        CancellationToken cancellationToken = default) =>
        PolicyGate.InterceptInputAsync(data, PreparePolicyContext(workerId, ws, "input"), cancellationToken);

    /// <summary>
    /// Capture a browser's current authorization generation, so a decision taken
    /// now can be refused later if the lease moved in between. Port of Go
    /// <c>BrowserInputFence</c>.
    /// </summary>
    public (long Generation, bool Allowed) BrowserInputFence(string workerId, object browser)
    {
        lock (SharedLock)
        {
            var st = Registry.Get(workerId);
            if (st?.WorkerWs is null || st.HijackPending is not null) return (0, false);
            if (!st.Browsers.ContainsKey(browser)
                || browser is IAbortableBrowserWs { IsActive: false }
                || !CanSendInput(st, browser))
            {
                return (0, false);
            }

            return (st.HijackOwnershipVersion, true);
        }
    }

    /// <summary>
    /// Register a pending approval for <paramref name="command"/>, park
    /// <paramref name="ws"/> so its further keystrokes are buffered rather than
    /// forwarded, and fan an <c>approval_pending</c> frame out to every browser
    /// of the worker. Port of Go <c>ParkBrowserForApproval</c>.
    /// </summary>
    /// <remarks>
    /// Ownership is revalidated here, not just at the fence: the gate may have
    /// awaited a remote governance service, and the lease can have moved while it
    /// did. Parking a browser that may no longer type would hold its keystrokes
    /// for a decision that could never be carried out.
    /// </remarks>
    public async Task<ApprovalParkResult> ParkBrowserForApprovalAsync(
        string workerId,
        object ws,
        string command,
        PolicyDecision decision,
        long? ownershipGeneration = null,
        CancellationToken cancellationToken = default)
    {
        var requestId = string.IsNullOrEmpty(decision.RequestId)
            ? Guid.NewGuid().ToString("N")
            : decision.RequestId;
        var now = Clock.Wall();
        var expiresAt = now + decision.TimeoutS;

        ApprovalRequest request;
        lock (SharedLock)
        {
            var st = Registry.Get(workerId);
            var generation = ownershipGeneration ?? st?.HijackOwnershipVersion ?? 0;
            if (st is null || st.HijackOwnershipVersion != generation || !CanSendInput(st, ws))
            {
                return new ApprovalParkResult(null, ApprovalParkReasons.OwnershipInvalid);
            }

            request = new ApprovalRequest
            {
                Id = requestId,
                WorkerId = workerId,
                SubmitterId = BrowserPrincipals.GetValueOrDefault(ws) ?? "anonymous",
                Command = command,
                Status = ApprovalStatus.Pending,
                CreatedAt = now,
                ExpiresAt = expiresAt,
                OriginBrowser = ws,
                OriginGeneration = generation,
            };
            if (!Approvals.Add(request))
            {
                return new ApprovalParkResult(null, ApprovalParkReasons.DuplicateId);
            }

            PausedBrowsers.Add(ws);
        }

        await Conn.BroadcastToBrowsersAsync(
            workerId,
            new Dictionary<string, object?>
            {
                ["type"] = "approval_pending",
                ["command"] = command,
                ["request_id"] = requestId,
                ["expires_at"] = expiresAt,
            },
            cancellationToken).ConfigureAwait(false);
        return new ApprovalParkResult(requestId, string.Empty);
    }

    /// <summary>
    /// Resolve a pending approval exactly once: claim it, then inject the held
    /// command (approve) or broadcast the red rejection banner (reject), release
    /// the parked browser — replaying its buffered keystrokes only on approve —
    /// and publish <c>approval_resolved</c>. Port of Go <c>ResolveApproval</c>.
    /// </summary>
    /// <remarks>
    /// <paramref name="resolverSubjectId"/> is used only for the audit line; the
    /// self-approval refusal lives in the REST route, as it does in Go and the
    /// reference.
    /// </remarks>
    public async Task<ApprovalResolution> ResolveApprovalAsync(
        string requestId,
        bool approve,
        string? reason,
        string? resolverSubjectId,
        CancellationToken cancellationToken = default)
    {
        var req = Approvals.Get(requestId);
        if (req is null) return ApprovalResolution.NotPending;

        var status = approve ? ApprovalStatus.Approved : ApprovalStatus.Rejected;
        if (!Approvals.ClaimRevision(requestId, req.Revision, status))
        {
            // Already resolved by a concurrent or prior call: idempotent no-op.
            return ApprovalResolution.NotPending;
        }

        if (approve)
        {
            var refused = await InjectApprovedCommandAsync(req, resolverSubjectId, cancellationToken)
                .ConfigureAwait(false);
            if (refused is { } outcome) return outcome;
        }
        else
        {
            await Conn.BroadcastToBrowsersAsync(
                req.WorkerId,
                new Dictionary<string, object?>
                {
                    ["type"] = "term",
                    ["data"] = RejectMessage(req.Command, reason),
                    ["ts"] = Clock.Wall(),
                },
                cancellationToken).ConfigureAwait(false);
            ReleaseParkedBrowser(req.OriginBrowser, replay: false);
        }

        // Re-check the opaque record identity before publishing a terminal
        // outcome: a long-running resolver may outlive pruning and id reuse.
        if (!Approvals.SetStatusRevision(requestId, req.Revision, status)) return ApprovalResolution.Resolved;

        var resolvedOutcome = approve ? "approved" : "rejected";
        await BroadcastApprovalResolvedAsync(req.WorkerId, requestId, resolvedOutcome, cancellationToken)
            .ConfigureAwait(false);
        Log("info", $"approval_resolved request_id={requestId} worker_id={req.WorkerId} "
            + $"approved={approve} outcome={resolvedOutcome} resolver={resolverSubjectId ?? "unknown"}");
        return ApprovalResolution.Resolved;
    }

    /// <summary>
    /// Deliver an approved command (and the submitter's buffered replay) under a
    /// single worker reservation. Returns non-null when the approval terminated
    /// here — nothing reached the worker, so the outcome is <c>refused</c>.
    /// </summary>
    private async Task<ApprovalResolution?> InjectApprovedCommandAsync(
        ApprovalRequest req,
        string? resolverSubjectId,
        CancellationToken cancellationToken)
    {
        var (delivered, total) = await Lease.SendApprovedBrowserInputAtGenerationAsync(
                req.WorkerId,
                req.OriginBrowser,
                req.OriginGeneration,
                req.Command,
                replay => ReleaseParkedBrowser(req.OriginBrowser, replay),
                cancellationToken)
            .ConfigureAwait(false);

        if (delivered == 0)
        {
            if (!Approvals.SetStatusRevision(req.Id, req.Revision, ApprovalStatus.Refused))
            {
                return ApprovalResolution.Resolved;
            }

            await BroadcastApprovalResolvedAsync(req.WorkerId, req.Id, "refused", cancellationToken)
                .ConfigureAwait(false);
            Log("info", $"approval_resolved request_id={req.Id} worker_id={req.WorkerId} "
                + $"approved=false outcome=refused resolver={resolverSubjectId ?? "unknown"}");
            return ApprovalResolution.Refused;
        }

        if (delivered < total)
        {
            // The command landed; only the replay did not. The approval stays
            // approved — the thing an admin decided on did run.
            Log("warning", $"approval_replay_failed request_id={req.Id} worker_id={req.WorkerId} "
                + $"delivered={delivered} total={total}");
        }

        return null;
    }

    private Task BroadcastApprovalResolvedAsync(
        string workerId,
        string requestId,
        string outcome,
        CancellationToken cancellationToken) =>
        Conn.BroadcastToBrowsersAsync(
            workerId,
            new Dictionary<string, object?>
            {
                ["type"] = "approval_resolved",
                ["outcome"] = outcome,
                ["request_id"] = requestId,
            },
            cancellationToken);

    /// <summary>
    /// Unpark a browser and take its hold buffer, returning the buffered
    /// keystrokes only when they are to be replayed. Port of Go
    /// <c>releaseParkedBrowser</c>.
    /// </summary>
    internal string ReleaseParkedBrowser(object? ws, bool replay)
    {
        if (ws is null) return string.Empty;
        lock (SharedLock)
        {
            PausedBrowsers.Remove(ws);
            HoldBuffers.Remove(ws, out var buffered);
            return replay ? buffered ?? string.Empty : string.Empty;
        }
    }

    /// <summary>Release whichever browser a now-expired approval was holding.</summary>
    private void ReleaseBrowserParkedFor(string requestId) =>
        ReleaseParkedBrowser(Approvals.Get(requestId)?.OriginBrowser, replay: false);

    /// <summary>
    /// The red terminal rejection banner. Byte-identical to Go's
    /// <c>rejectMessage</c> and the reference's deny branch.
    /// </summary>
    internal static string RejectMessage(string command, string? reason)
    {
        var message = "\r\u001b[31m[REJECTED] Command '" + command.Trim() + "' blocked by Admin.\u001b[0m";
        if (!string.IsNullOrEmpty(reason))
        {
            message += " \u001b[33mReason: " + reason + "\u001b[0m";
        }

        return message + "\r";
    }
}
