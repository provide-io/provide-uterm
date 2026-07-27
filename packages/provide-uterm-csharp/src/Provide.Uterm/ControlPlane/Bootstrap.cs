//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.ControlPlane;

/// <summary>Raised for an unrecognised control-plane backend. Mirrors the
/// Python ValueError("unsupported control-plane backend: ...").</summary>
public sealed class ControlPlaneConfigurationException(string message) : Exception(message);

/// <summary>
/// Selects a control-plane engine for the configured backend. Port of
/// provide.uterm.control.plane.bootstrap.bootstrap_control_plane and the Go
/// controlplane/bootstrap package.
/// </summary>
public static class Bootstrap
{
    /// <summary>
    /// Constructs an engine for <paramref name="backend"/>. The caller owns the
    /// lifecycle: open, migrate, and dispose.
    /// </summary>
    public static IEngine New(string? backend, string? databaseUrl)
    {
        var selected = string.IsNullOrWhiteSpace(backend) ? "memory" : backend.Trim().ToLowerInvariant();
        return selected switch
        {
            "memory" => new MemoryEngine(),
            "sqlite" => new SqliteEngine(
                string.IsNullOrWhiteSpace(databaseUrl) ? ":memory:" : databaseUrl),
            _ => throw new ControlPlaneConfigurationException(
                "unsupported control-plane backend: " + backend),
        };
    }

    /// <summary>
    /// Constructs an engine and readies it for use — open, then migrate. Migrate
    /// matters: nothing read a store before the graphical-target registry, so a
    /// missing schema would surface only at first use.
    /// </summary>
    public static async Task<IEngine> OpenAsync(
        string? backend, string? databaseUrl, CancellationToken ct = default)
    {
        var engine = New(backend, databaseUrl);
        try
        {
            await engine.OpenAsync(ct).ConfigureAwait(false);
            await engine.MigrateAsync(ct).ConfigureAwait(false);
            return engine;
        }
        catch
        {
            await engine.CloseAsync(CancellationToken.None).ConfigureAwait(false);
            throw;
        }
    }
}
