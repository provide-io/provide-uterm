//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Text;
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.ServerAuth;

/// <summary>
/// A session's share viewers (share:{id}:... subjects holding only the viewer
/// role) can be revoked: tokens issued at or before the cutoff are refused for
/// reads from then on, whatever the provider or the session's visibility says.
/// Port of tests/server/test_viewer_revocation.py's cutoff half.
/// </summary>
public sealed class ShareViewerRevocationTests
{
    private static Principal Viewer(string sessionId, object? iat, params string[] roles)
    {
        var p = new Principal
        {
            SubjectId = $"share:{sessionId}:blackbetty:7",
            Roles = StringSet.Of(roles.Length == 0 ? new[] { "viewer" } : roles),
        };
        if (iat is not null) p.Claims["iat"] = iat;
        return p;
    }

    private static Principal NoRoles(string sessionId) =>
        new() { SubjectId = $"share:{sessionId}:blackbetty:7", Roles = new StringSet() };

    private static SessionDefinition Session(string id, string visibility = "public") =>
        new() { SessionId = id, DisplayName = id, ConnectorType = "shell", Visibility = visibility };

    [Fact]
    public void Who_counts_as_a_share_viewer()
    {
        Assert.True(AuthorizationService.IsShareViewer(Viewer("s1", 1.0), "s1"));
        Assert.True(AuthorizationService.IsShareViewer(NoRoles("s1"), "s1"));
        Assert.False(AuthorizationService.IsShareViewer(Viewer("s2", 1.0), "s1"));
        Assert.False(AuthorizationService.IsShareViewer(Viewer("s1x", 1.0), "s1"));
        Assert.False(AuthorizationService.IsShareViewer(Viewer("s1", 1.0, "viewer", "operator"), "s1"));
        Assert.False(AuthorizationService.IsShareViewer(Viewer("s1", 1.0, "admin"), "s1"));
        var scoped = Viewer("s1", 1.0);
        scoped.AdminSessionScope = "s1";
        Assert.False(AuthorizationService.IsShareViewer(scoped, "s1"));
        Assert.False(AuthorizationService.IsShareViewer(
            new Principal { SubjectId = "alice", Roles = StringSet.Of("viewer") }, "s1"));
    }

    [Fact]
    public void Tokens_issued_at_or_before_the_cutoff_are_revoked()
    {
        var authz = new AuthorizationService();
        Assert.False(authz.ShareViewerRevoked(Viewer("s1", 1.0), "s1"));
        Assert.Null(authz.ShareViewerRevocation("s1"));

        Assert.Equal(500.5, authz.RevokeShareViewers("s1", 500.5));
        Assert.Equal(500.5, authz.ShareViewerRevocation("s1"));

        Assert.True(authz.ShareViewerRevoked(Viewer("s1", 499.0), "s1"));
        Assert.True(authz.ShareViewerRevoked(Viewer("s1", 500.5), "s1"));
        Assert.False(authz.ShareViewerRevoked(Viewer("s1", 501.0), "s1"));
        Assert.True(authz.ShareViewerRevoked(Viewer("s1", 500L), "s1"));
        Assert.False(authz.ShareViewerRevoked(Viewer("s1", 501L), "s1"));
        Assert.True(authz.ShareViewerRevoked(Viewer("s1", 500), "s1"));
        Assert.False(authz.ShareViewerRevoked(Viewer("s1", 501), "s1"));
        // No numeric iat: the token cannot show it postdates the cutoff.
        Assert.True(authz.ShareViewerRevoked(Viewer("s1", null), "s1"));
        Assert.True(authz.ShareViewerRevoked(Viewer("s1", "600"), "s1"));

        Assert.False(authz.ShareViewerRevoked(Viewer("s2", 1.0), "s2"));
        Assert.False(authz.ShareViewerRevoked(Viewer("s1", 1.0, "admin"), "s1"));

        authz.RevokeShareViewers("s1", 900);
        Assert.True(authz.ShareViewerRevoked(Viewer("s1", 600.0), "s1"));
    }

    [Fact]
    public void The_cutoff_table_is_bounded_and_forgets_the_oldest()
    {
        var authz = new AuthorizationService();
        for (var i = 0; i < AuthorizationService.ShareViewerRevocationsMax; i++)
        {
            authz.RevokeShareViewers("s" + i, i);
        }
        authz.RevokeShareViewers("s0", 1e6);
        authz.RevokeShareViewers("overflow", 2e6);
        Assert.Null(authz.ShareViewerRevocation("s1"));
        Assert.Equal(1e6, authz.ShareViewerRevocation("s0"));
        Assert.Equal(2.0, authz.ShareViewerRevocation("s2"));
        Assert.Equal(2e6, authz.ShareViewerRevocation("overflow"));
    }

    private sealed class AllowAllReads : IAuthorizationProvider
    {
        private readonly LocalAuthorizationProvider _local = new();
        public StringSet CapabilitiesFor(Principal p) => _local.CapabilitiesFor(p);
        public bool HasCapability(Principal p, string capability) => _local.HasCapability(p, capability);
        public bool IsAdmin(Principal p) => _local.IsAdmin(p);
        public bool IsOwner(Principal p, SessionDefinition session) => _local.IsOwner(p, session);
        public bool CanReadSession(Principal p, SessionDefinition session) => true;
        public bool CanReadRecording(Principal p, SessionDefinition session) => true;
        public bool CanCreateSession(Principal p) => _local.CanCreateSession(p);
        public bool CanMutateSession(Principal p, SessionDefinition session, string action) =>
            _local.CanMutateSession(p, session, action);
        public string ResolveBrowserRole(Principal p, SessionDefinition session) => _local.ResolveBrowserRole(p, session);
    }

    [Fact]
    public void A_revoked_share_viewer_cannot_read_whatever_the_provider_says()
    {
        foreach (var authz in new[] { new AuthorizationService(), new AuthorizationService(new AllowAllReads()) })
        {
            var viewer = Viewer("s1", 100.0);
            Assert.True(authz.CanReadSession(viewer, Session("s1")));
            Assert.True(authz.CanReadRecording(viewer, Session("s1")));

            authz.RevokeShareViewers("s1", 200);

            Assert.False(authz.CanReadSession(viewer, Session("s1")));
            Assert.False(authz.CanReadRecording(viewer, Session("s1")));
            var fresh = Viewer("s1", 300.0);
            Assert.True(authz.CanReadSession(fresh, Session("s1", "private")));
            Assert.True(authz.CanReadRecording(fresh, Session("s1", "private")));
        }
    }

    private const string Secret = "uterm-test-secret-32-byte-minimum-key";

    private static string Mint(DateTime? issuedAt)
    {
        var now = DateTime.UtcNow;
        var token = new JwtSecurityTokenHandler().CreateJwtSecurityToken(new SecurityTokenDescriptor
        {
            Issuer = "provide-uterm",
            Audience = "provide-uterm-server",
            Subject = new ClaimsIdentity(new[] { new Claim("sub", "share:s1:blackbetty:7"), new Claim("roles", "viewer") }),
            NotBefore = now.AddMinutes(-1),
            Expires = now.AddMinutes(5),
            IssuedAt = issuedAt,
            SigningCredentials = new SigningCredentials(
                new SymmetricSecurityKey(Encoding.UTF8.GetBytes(Secret)), SecurityAlgorithms.HmacSha256),
        });
        if (issuedAt is null) token.Payload.Remove("iat");
        return new JwtSecurityTokenHandler().WriteToken(token);
    }

    [Fact]
    public void A_jwt_principal_carries_its_iat_so_a_cutoff_can_judge_it()
    {
        var idp = new LocalIdentityProvider(new AuthConfig
        {
            Mode = "jwt",
            JwtPublicKeyPem = Secret,
            JwtAlgorithms = new List<string> { "HS256" },
        });
        var issued = new DateTime(2026, 10, 13, 12, 0, 0, DateTimeKind.Utc);

        var withIat = idp.PrincipalFromJwtToken(Mint(issued));
        Assert.Equal((double)new DateTimeOffset(issued).ToUnixTimeSeconds(), withIat.Claims["iat"]);

        var authz = new AuthorizationService();
        authz.RevokeShareViewers("s1", new DateTimeOffset(issued).ToUnixTimeSeconds() + 0.5);
        Assert.True(authz.ShareViewerRevoked(withIat, "s1"));

        var withoutIat = idp.PrincipalFromJwtToken(Mint(null));
        Assert.False(withoutIat.Claims.ContainsKey("iat"));
        Assert.True(authz.ShareViewerRevoked(withoutIat, "s1"));
    }
}
