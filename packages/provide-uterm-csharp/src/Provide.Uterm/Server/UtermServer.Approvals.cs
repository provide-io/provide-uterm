//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;

namespace Provide.Uterm.Server;

/// <summary>
/// The browser-input approval pipeline: the policy gate that can hold a
/// keystroke for an admin decision, the parked browser whose further keystrokes
/// are buffered rather than forwarded, and the sweep that retires a decision
/// nobody made. Port of Go <c>server/approvals_flow.go</c> + <c>sweeps.go</c>.
/// </summary>
public sealed partial class UtermServer
{
    /// <summary>How often expired approvals are retired. Matches Go's <c>approvalSweepInterval</c>.</summary>
    private static readonly TimeSpan DefaultApprovalSweepInterval = TimeSpan.FromSeconds(30);

    private CancellationTokenSource? _approvalSweepCts;
    private Task? _approvalSweepTask;

    /// <summary>
    /// Process one browser input frame through the approval pipeline.
    /// </summary>
    /// <remarks>
    /// Order matters, and it is Go's:
    /// <list type="number">
    /// <item>a parked browser buffers, and is told when the buffer overflows;</item>
    /// <item>the no-op gate — every deployment that configures none — forwards
    /// directly, exactly as this seam did before the pipeline existed;</item>
    /// <item>otherwise the length cap, then the capability fence, then the gate:
    /// <c>hold</c> parks and broadcasts <c>approval_pending</c>, <c>deny</c>
    /// answers with an error frame, <c>allow</c> forwards at the fenced
    /// generation.</item>
    /// </list>
    /// Divergence from Go, which applies its <c>MaxInputChars</c> cap ahead of
    /// the no-op fast path: this port has never enforced that cap on any input
    /// path, so enforcing it there would silently start dropping oversized
    /// keystrokes for every existing ungated deployment. It applies only where
    /// the cap is load-bearing — a gated input becomes a stored approval command
    /// and a hold buffer.
    /// </remarks>
    private async Task HandleBrowserInputAsync(
        string workerId,
        BrowserWsConn conn,
        string text,
        CancellationToken ct)
    {
        // The parked check and the buffer append are one hub transition. If an
        // approval concurrently unparked this browser, fall through to normal
        // fenced delivery rather than dropping the keystroke.
        var (held, tooLong) = _deps.Hub.TryHoldBrowserInput(conn, text);
        if (held)
        {
            if (tooLong) await SendInputErrorAsync(conn, "Input too long.", ct).ConfigureAwait(false);
            return;
        }

        if (_deps.Hub.IsNoOpPolicyGate)
        {
            _ = await SendBrowserInputAsync(workerId, conn, text, ct).ConfigureAwait(false);
            return;
        }

        if (text.Length > _deps.Hub.MaxInputChars)
        {
            await SendInputErrorAsync(conn, "Input too long.", ct).ConfigureAwait(false);
            return;
        }

        var (generation, allowed) = _deps.Hub.BrowserInputFence(workerId, conn);
        if (!allowed) return;

        PolicyDecision decision;
        try
        {
            decision = await _deps.Hub.InterceptBrowserInputAsync(workerId, conn, text, ct)
                .ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            // A gate that cannot decide must not become a way to type unchecked:
            // the input is dropped and the socket stays usable.
            _deps.Hub.Log("warning", $"intercept_input_failed worker_id={workerId} error={ex.Message}");
            return;
        }

        switch (decision.Action)
        {
            case PolicyActions.Hold:
                var parked = await _deps.Hub.ParkBrowserForApprovalAsync(
                    workerId, conn, text, decision, generation, ct).ConfigureAwait(false);
                if (parked.RequestId is null)
                {
                    _deps.Hub.Log(
                        "warning",
                        $"park_for_approval_failed worker_id={workerId} reason={parked.Reason}");
                }

                break;
            case PolicyActions.Allow:
                _ = await _deps.Hub.Lease.SendBrowserInputAtGenerationAsync(
                    workerId, conn, generation, text, ct).ConfigureAwait(false);
                break;
            default:
                await SendInputErrorAsync(conn, "Command blocked by policy: " + text, ct)
                    .ConfigureAwait(false);
                break;
        }
    }

    private static Task SendInputErrorAsync(BrowserWsConn conn, string message, CancellationToken ct) =>
        conn.SendTextAsync(
            ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
            {
                ["type"] = "error",
                ["message"] = message,
            }),
            ct);

    /// <summary>
    /// Start the approval sweep. Port of Go <c>StartSweeps</c>'s approval leg:
    /// pending requests past their deadline become <c>timeout</c> (releasing the
    /// browser they parked), and long-settled ones are pruned.
    /// </summary>
    private void StartApprovalSweep()
    {
        var interval = _deps.ApprovalSweepInterval ?? DefaultApprovalSweepInterval;
        if (interval <= TimeSpan.Zero) return;
        _approvalSweepCts = new CancellationTokenSource();
        _approvalSweepTask = RunApprovalSweepAsync(interval, _approvalSweepCts.Token);
    }

    private async Task RunApprovalSweepAsync(TimeSpan interval, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(interval, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return; // Stopping the server stops the sweep.
            }

            _deps.Hub.Approvals.CleanupExpired();
        }
    }

    private async Task StopApprovalSweepAsync()
    {
        if (_approvalSweepCts is null) return;
        await _approvalSweepCts.CancelAsync().ConfigureAwait(false);
        if (_approvalSweepTask is not null)
        {
            await _approvalSweepTask.ConfigureAwait(false);
            _approvalSweepTask = null;
        }

        _approvalSweepCts.Dispose();
        _approvalSweepCts = null;
    }
}
