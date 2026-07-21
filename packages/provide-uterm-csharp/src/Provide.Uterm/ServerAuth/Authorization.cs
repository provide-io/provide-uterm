//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.ServerAuth;

/// <summary>Pluggable authorization decision surface (Go AuthorizationProvider parity).</summary>
public interface IAuthorizationProvider
{
    StringSet CapabilitiesFor(Principal p);
    bool HasCapability(Principal p, string capability);
    bool IsAdmin(Principal p);
    bool IsOwner(Principal p, SessionDefinition session);
    bool CanReadSession(Principal p, SessionDefinition session);
    bool CanReadRecording(Principal p, SessionDefinition session);
    bool CanCreateSession(Principal p);
    bool CanMutateSession(Principal p, SessionDefinition session, string action);
    string ResolveBrowserRole(Principal p, SessionDefinition session);
}

/// <summary>Local RBAC authorization provider.</summary>
public sealed class LocalAuthorizationProvider : IAuthorizationProvider
{
    public StringSet CapabilitiesFor(Principal p)
    {
        var roleCaps = new StringSet();
        foreach (var role in p.Roles)
        {
            if (AuthorizationService.RoleCapabilities.TryGetValue(role, out var caps))
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

/// <summary>
/// Authorization gateway. Default constructor uses local RBAC;
/// <see cref="FromConfig"/> selects webhook authz when governance.authz_webhook_url is set.
/// </summary>
public sealed class AuthorizationService
{
    public static readonly IReadOnlyDictionary<string, StringSet> RoleCapabilities = new Dictionary<string, StringSet>
    {
        ["viewer"] = StringSet.Of(
            "session.read",
            "session.recording.read",
            "graphical.target.read"),
        ["operator"] = StringSet.Of(
            "session.read", "session.recording.read",
            "session.control.create", "session.control.connect",
            "session.control.mode", "session.control.clear", "session.control.update",
            "graphical.target.read",
            "graphical.target.manage",
            "graphical.session.attach"),
        ["admin"] = StringSet.Of(
            "session.read", "session.recording.read",
            "session.control.create", "session.control.connect",
            "session.control.mode", "session.control.clear", "session.control.update",
            "session.control.delete", "session.control.hijack",
            "graphical.target.read",
            "graphical.target.manage",
            "graphical.session.attach"),
    };

    private readonly IAuthorizationProvider _provider;

    /// <summary>Local RBAC only.</summary>
    public AuthorizationService() : this(new LocalAuthorizationProvider())
    {
    }

    /// <summary>Build with a custom provider (e.g. <see cref="WebhookAuthorizationProvider"/>).</summary>
    public AuthorizationService(IAuthorizationProvider provider)
    {
        _provider = provider ?? throw new ArgumentNullException(nameof(provider));
    }

    /// <summary>
    /// Pick local RBAC or webhook authz from config.
    /// When <c>governance.authz_webhook_url</c> is set, returns a webhook-backed service.
    /// </summary>
    public static AuthorizationService FromConfig(UtermServerConfig? cfg)
    {
        if (cfg is null || string.IsNullOrWhiteSpace(cfg.Governance.AuthzWebhookUrl))
        {
            return new AuthorizationService();
        }

        var g = cfg.Governance;
        var provider = new WebhookAuthorizationProvider(
            g.AuthzWebhookUrl!,
            g.AuthzWebhookSecret ?? "",
            g.AuthzWebhookTimeoutS);
        return new AuthorizationService(provider);
    }

    public StringSet CapabilitiesFor(Principal p) => _provider.CapabilitiesFor(p);

    public bool HasCapability(Principal p, string capability) => _provider.HasCapability(p, capability);

    public bool IsAdmin(Principal p) => _provider.IsAdmin(p);

    /// <summary>Direct role-membership check (never delegated to webhook).</summary>
    public bool HasRole(Principal p, string role) => p.Roles.Has(role);

    public bool IsOwner(Principal p, SessionDefinition session) => _provider.IsOwner(p, session);

    public bool CanReadSession(Principal p, SessionDefinition session) => _provider.CanReadSession(p, session);

    /// <summary>Read session recording meta/entries/download (capability + session read).</summary>
    public bool CanReadRecording(Principal p, SessionDefinition session) => _provider.CanReadRecording(p, session);

    public bool CanCreateSession(Principal p) => _provider.CanCreateSession(p);

    public bool CanMutateSession(Principal p, SessionDefinition session, string action) =>
        _provider.CanMutateSession(p, session, action);

    public string ResolveBrowserRole(Principal p, SessionDefinition session) =>
        _provider.ResolveBrowserRole(p, session);
}
