//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text;

namespace Provide.Uterm.Server;

/// <summary>Shared worker/tunnel bearer parsing and constant-time token validation.</summary>
internal static class WorkerBearerAuthentication
{
    private const string SchemePrefix = "Bearer ";

    internal static bool IsAuthorized(string? authorization, string? expectedToken)
    {
        // Preserve the existing C# configuration contract: null or empty means
        // the optional worker credential gate is disabled.
        if (string.IsNullOrEmpty(expectedToken)) return true;

        var provided = authorization is not null
            && authorization.StartsWith(SchemePrefix, StringComparison.Ordinal)
                ? authorization[SchemePrefix.Length..].Trim()
                : string.Empty;

        return CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(provided),
            Encoding.UTF8.GetBytes(expectedToken));
    }
}
