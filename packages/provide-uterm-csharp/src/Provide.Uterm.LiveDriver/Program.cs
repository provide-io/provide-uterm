//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Runtime.InteropServices;
using Provide.Uterm.Conformance;
using Provide.Telemetry;

ProvideTelemetry.SetupTelemetry();
try
{
    // Signals are the harness's other way to stop a server driver; closing stdin is
    // the ordinary one. Both end up cancelling the same token.
    using var stopping = new CancellationTokenSource();
    using var sigterm = PosixSignalRegistration.Create(PosixSignal.SIGTERM, ctx =>
    {
        ctx.Cancel = true;
        stopping.Cancel();
    });
    using var sigint = PosixSignalRegistration.Create(PosixSignal.SIGINT, ctx =>
    {
        ctx.Cancel = true;
        stopping.Cancel();
    });

    return await LiveDriver.ExecuteAsync(
        args,
        input: Console.OpenStandardInput(),
        ct: stopping.Token).ConfigureAwait(false);
}
finally
{
    ProvideTelemetry.ShutdownTelemetry();
}
