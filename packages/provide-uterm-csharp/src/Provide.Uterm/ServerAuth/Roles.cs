//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.ServerAuth;

/// <summary>Canonical RBAC allow-list for roles.</summary>
public static class AuthRoles
{
    public static readonly StringSet KnownRoles = StringSet.Of("viewer", "operator", "admin");
    public const string DefaultRole = "viewer";

    public static StringSet FilterKnownRoles(IEnumerable<string> roles)
    {
        var allowed = new StringSet();
        foreach (var role in roles)
        {
            var r = role.Trim().ToLowerInvariant();
            if (r.Length == 0) continue;
            if (KnownRoles.Has(r))
            {
                allowed.Add(r);
            }
        }

        return allowed.Count == 0 ? StringSet.Of(DefaultRole) : allowed;
    }
}
