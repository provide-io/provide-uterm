//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Manager;
using Provide.Telemetry;

ProvideTelemetry.SetupTelemetry();
try
{
    return ManagerHost.Run(args);
}
finally
{
    ProvideTelemetry.ShutdownTelemetry();
}
