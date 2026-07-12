//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.ServerAuth;

/// <summary>Local RBAC authorization service.</summary>
public sealed class AuthorizationService
{
    public static readonly IReadOnlyDictionary<string, StringSet> RoleCapabilities = new Dictionary<string, StringSet>
    {
        ["viewer"] = StringSet.Of("session.read", "session.recording.read"),
        ["operator"] = StringSet.Of(
            "session.read", "session.recording.read",
            "session.control.create", "session.control.connect",
            "session.control.mode", "session.control.clear", "session.control.update"),
        ["admin"] = StringSet.Of(
            "session.read", "session.recording.read",
            "session.control.create", "session.control.connect",
            "session.control.mode", "session.control.clear", "session.control.update",
            "session.control.delete", "session.control.hijack"),
    };

    public StringSet CapabilitiesFor(Principal p)
    {
        var roleCaps = new StringSet();
        foreach (var role in p.Roles)
        {
            if (RoleCapabilities.TryGetValue(role, out var caps))
            {
                foreach (var c in caps) roleCaps.Add(c);
            }
        }

        if (p.Scopes.Count > 0 && !p.Scopes.Has("*"))
        {
            var narrowed = new StringSet();
            foreach (var cap in roleCaps)
            {
                if (p.Scopes.Has(cap)) narrowed.Add(cap);
            }

            return narrowed;
        }

        return roleCaps;
    }

    public bool HasCapability(Principal p, string capability) => CapabilitiesFor(p).Has(capability);

    public bool IsAdmin(Principal p) => p.Roles.Has("admin") && p.AdminSessionScope is null;

    public bool HasRole(Principal p, string role) => p.Roles.Has(role);

    public bool IsOwner(Principal p, SessionDefinition session) =>
        session.Owner is not null && session.Owner == p.SubjectId;

    private static bool IsAdminForSession(Principal p, SessionDefinition session) =>
        p.Roles.Has("admin") && (p.AdminSessionScope is null || p.AdminSessionScope == session.SessionId);

    public bool CanReadSession(Principal p, SessionDefinition session)
    {
        if (!HasCapability(p, "session.read")) return false;
        if (IsAdminForSession(p, session) || IsOwner(p, session)) return true;
        if (p.SubjectId.StartsWith($"share:{session.SessionId}:", StringComparison.Ordinal)) return true;
        return session.Visibility switch
        {
            "public" => true,
            "operator" => p.Roles.Has("operator"),
            _ => false,
        };
    }

    /// <summary>Read session recording meta/entries/download (capability + session read).</summary>
    public bool CanReadRecording(Principal p, SessionDefinition session) =>
        CanReadSession(p, session) && HasCapability(p, "session.recording.read");

    public bool CanCreateSession(Principal p) => HasCapability(p, "session.control.create");

    public bool CanMutateSession(Principal p, SessionDefinition session, string action)
    {
        if (!HasCapability(p, action)) return false;
        if (IsAdminForSession(p, session)) return true;
        if (session.Owner is null) return false;
        return IsOwner(p, session);
    }

    public string ResolveBrowserRole(Principal p, SessionDefinition session)
    {
        if (!CanReadSession(p, session)) return "viewer";
        if (IsAdminForSession(p, session)) return "admin";
        if (CanMutateSession(p, session, "session.control.hijack") || p.Roles.Has("operator")) return "operator";
        return "viewer";
    }
}
