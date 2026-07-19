//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Policy;

/// <summary>
/// Pure policy evaluation shared with Python/Go (spec/behavior.json).
/// </summary>
public interface IPolicyEngine
{
    string? CanInject(string sessionId, string leaseId, string principalRole);
    string? CanPerform(string op, string role, bool leaseOwned, bool sessionActive);
}

public sealed class StrictPolicyEngine : IPolicyEngine
{
    public const string ErrInsufficientRole = "forbidden: insufficient role";
    public const string ErrNoActiveLease = "forbidden: no active lease";
    public const string ErrSessionInactive = "forbidden: session inactive";

    private static readonly Dictionary<string, int> RoleRank = new()
    {
        ["viewer"] = 0,
        ["operator"] = 1,
        ["admin"] = 2,
    };

    private static readonly Dictionary<string, string> OpMinRole = new()
    {
        ["input_inject"] = "operator",
        ["hijack_step"] = "operator",
        ["hijack_release"] = "operator",
        ["hijack_acquire"] = "operator",
    };

    public string? CanInject(string sessionId, string leaseId, string principalRole)
    {
        _ = sessionId;
        return CanPerform("input_inject", principalRole, !string.IsNullOrEmpty(leaseId), sessionActive: true);
    }

    public string? CanPerform(string op, string role, bool leaseOwned, bool sessionActive)
    {
        if (!OpMinRole.TryGetValue(op, out var minRole))
        {
            return "forbidden: unknown operation " + op;
        }

        if (!RoleOk(role, minRole))
        {
            return ErrInsufficientRole;
        }

        if ((op is "input_inject" or "hijack_step") && !leaseOwned)
        {
            return ErrNoActiveLease;
        }

        if ((op is "hijack_step" or "hijack_acquire") && !sessionActive)
        {
            return ErrSessionInactive;
        }

        return null;
    }

    private static bool RoleOk(string role, string minimum) =>
        RoleRank.TryGetValue(role, out var rr)
        && RoleRank.TryGetValue(minimum, out var mr)
        && rr >= mr;
}
