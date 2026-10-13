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
    public void A_full_cutoff_table_refuses_new_sessions_instead_of_evicting_one()
    {
        var authz = new AuthorizationService();
        const double Base = 1e9;
        for (var i = 0; i < AuthorizationService.ShareViewerRevocationsMax; i++)
        {
            authz.RevokeShareViewers("s" + i, Base + i);
        }
        // A session already holding a cutoff can always move it forward.
        Assert.Equal(Base + 1e4, authz.RevokeShareViewers("s0", Base + 1e4));
        // A new one is refused rather than pushing a live cutoff out.
        Assert.Throws<ShareViewerRevocationsFullException>(() => authz.RevokeShareViewers("overflow", Base + 2e4));
        Assert.Null(authz.ShareViewerRevocation("overflow"));
        Assert.Equal(Base + 1, authz.ShareViewerRevocation("s1"));
        // Once the oldest cutoffs have aged out there is room again.
        var later = Base + 1 + AuthorizationService.ShareViewerRevocationTtlSeconds + 0.5;
        Assert.Equal(later, authz.RevokeShareViewers("overflow", later));
        Assert.Null(authz.ShareViewerRevocation("s1"));
        Assert.Equal(Base + 1e4, authz.ShareViewerRevocation("s0"));
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

    [Fact]
    public void Cutoffs_age_out_after_a_day_and_are_forgotten_with_their_session()
    {
        Assert.Equal(86400, AuthorizationService.ShareViewerRevocationTtlSeconds);
        var authz = new AuthorizationService();
        authz.RevokeShareViewers("old", 1000);
        authz.RevokeShareViewers("edge", 2000);
        authz.RevokeShareViewers("new", 2000 + AuthorizationService.ShareViewerRevocationTtlSeconds);
        Assert.Null(authz.ShareViewerRevocation("old"));
        Assert.Equal(2000, authz.ShareViewerRevocation("edge"));

        authz.ForgetShareViewers("edge");
        authz.ForgetShareViewers("never-revoked");
        Assert.Null(authz.ShareViewerRevocation("edge"));
        Assert.NotNull(authz.ShareViewerRevocation("new"));
    }

    [Fact]
    public void A_jwt_issued_in_the_future_is_refused()
    {
        var idp = new LocalIdentityProvider(new AuthConfig
        {
            Mode = "jwt",
            JwtPublicKeyPem = Secret,
            JwtAlgorithms = new List<string> { "HS256" },
            ClockSkewSeconds = 15,
        });
        Assert.Throws<SecurityTokenException>(() => idp.PrincipalFromJwtToken(Mint(DateTime.UtcNow.AddHours(1))));
        Assert.True(idp.PrincipalFromJwtToken(Mint(DateTime.UtcNow.AddSeconds(5))).Claims.ContainsKey("iat"));
    }

    private const int Cap = AuthorizationService.ShareViewerRevocationsPerPrincipalMax;

    [Fact]
    public void One_principal_holds_at_most_its_cap_of_cutoffs_keeping_the_newest()
    {
        var authz = new AuthorizationService();
        for (var i = 0; i < Cap + 2; i++) authz.RevokeShareViewers("s" + i, 100 + i, "alice");
        // Alice's two oldest made room for her two newest.
        Assert.Null(authz.ShareViewerRevocation("s0"));
        Assert.Null(authz.ShareViewerRevocation("s1"));
        Assert.Equal(102, authz.ShareViewerRevocation("s2"));
        Assert.Equal(100 + Cap + 1, authz.ShareViewerRevocation("s" + (Cap + 1)));
        Assert.Equal(Cap, authz.ShareViewerRevocationCount);
    }

    [Fact]
    public void The_per_principal_cap_leaves_other_principals_alone()
    {
        var authz = new AuthorizationService();
        authz.RevokeShareViewers("bobs", 1, "bob");
        authz.RevokeShareViewers("admins", 1);
        for (var i = 0; i <= Cap; i++) authz.RevokeShareViewers("s" + i, 10 + i, "alice");
        Assert.Equal(1, authz.ShareViewerRevocation("bobs"));
        Assert.Equal(1, authz.ShareViewerRevocation("admins"));
    }

    [Fact]
    public void Moving_your_own_cutoff_forward_evicts_nothing()
    {
        var authz = new AuthorizationService();
        for (var i = 0; i < Cap; i++) authz.RevokeShareViewers("s" + i, 100 + i, "alice");
        authz.RevokeShareViewers("s0", 500, "alice");
        Assert.Equal(Cap, authz.ShareViewerRevocationCount);
        Assert.Equal(500, authz.ShareViewerRevocation("s0"));
        // s0 is now alice's newest, so her next new session pushes s1 out.
        authz.RevokeShareViewers("extra", 600, "alice");
        Assert.Equal(500, authz.ShareViewerRevocation("s0"));
        Assert.Null(authz.ShareViewerRevocation("s1"));
    }

    [Fact]
    public void A_cutoff_counts_against_whoever_revoked_it_last()
    {
        var authz = new AuthorizationService();
        authz.RevokeShareViewers("shared", 1, "bob");
        for (var i = 0; i < Cap - 1; i++) authz.RevokeShareViewers("a" + i, 10 + i, "alice");
        // Alice takes "shared" over from bob: her cap is now full, nothing evicted.
        authz.RevokeShareViewers("shared", 500, "alice");
        Assert.Equal(Cap, authz.ShareViewerRevocationCount);
        authz.RevokeShareViewers("bob2", 600, "bob");
        Assert.Equal(500, authz.ShareViewerRevocation("shared"));
        Assert.Equal(10, authz.ShareViewerRevocation("a0"));
        authz.RevokeShareViewers("a-next", 700, "alice");
        Assert.Null(authz.ShareViewerRevocation("a0"));
    }

    [Fact]
    public void An_uncapped_revoke_is_never_evicted_by_the_per_principal_cap()
    {
        var authz = new AuthorizationService();
        for (var i = 0; i < Cap + 5; i++) authz.RevokeShareViewers("s" + i, 100 + i);
        Assert.Equal(100, authz.ShareViewerRevocation("s0"));
        Assert.Equal(Cap + 5, authz.ShareViewerRevocationCount);
    }

    [Fact]
    public void A_principal_at_its_cap_makes_its_own_room_in_a_full_table()
    {
        var authz = new AuthorizationService();
        const double Base = 1e9;
        for (var i = 0; i < Cap; i++) authz.RevokeShareViewers("a" + i, Base + i, "alice");
        for (var i = 0; i < AuthorizationService.ShareViewerRevocationsMax - Cap; i++)
        {
            authz.RevokeShareViewers("s" + i, Base + Cap + i);
        }
        Assert.Equal(Base + 2e4, authz.RevokeShareViewers("a-new", Base + 2e4, "alice"));
        Assert.Null(authz.ShareViewerRevocation("a0"));
        Assert.Equal(Base + Cap, authz.ShareViewerRevocation("s0"));
    }

    [Fact]
    public void Ageing_out_and_forgetting_free_a_principals_slots()
    {
        var authz = new AuthorizationService();
        const double Ttl = AuthorizationService.ShareViewerRevocationTtlSeconds;
        authz.RevokeShareViewers("old", 1, "alice");
        authz.RevokeShareViewers("gone", 2 + Ttl, "alice");
        authz.ForgetShareViewers("gone");
        for (var i = 0; i < Cap; i++) authz.RevokeShareViewers("s" + i, 10 + Ttl + i, "alice");
        Assert.Equal(10 + Ttl, authz.ShareViewerRevocation("s0"));
        Assert.Equal(Cap, authz.ShareViewerRevocationCount);
    }
}
