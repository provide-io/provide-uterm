//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

public class ServerAuthCoverageTests
{
    [Fact]
    public async Task Header_Auth_Parses_Tenant_And_Invalid_Tenant_Is_Anonymous()
    {
        var cfg = new AuthConfig { Mode = "header" };
        var idp = new LocalIdentityProvider(cfg, new ApiKeyStore());

        var ok = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["x-uterm-principal"] = "alice",
                ["x-uterm-role"] = "admin",
                ["x-uterm-tenant"] = "tenant-1",
            },
        });
        Assert.Equal("alice", ok.SubjectId);
        Assert.Equal("tenant-1", ok.TenantId);
        Assert.True(ok.Roles.Has("admin"));

        var bad = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["x-uterm-principal"] = "alice",
                ["x-uterm-tenant"] = "bad tenant",
            },
        });
        Assert.Equal("anonymous", bad.SubjectId);
        Assert.Null(bad.TenantId);
    }

    [Fact]
    public async Task Jwt_Auth_Parses_Tenant_Claim()
    {
        var cfg = new AuthConfig { Mode = "jwt" };
        var token = DevIdp.Setup(cfg, new DevIdp.Options
        {
            Subject = "alice",
            Roles = new[] { "operator" },
            Tenant = "tenant-1",
        });

        var idp = new LocalIdentityProvider(cfg);
        var principal = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["Authorization"] = "Bearer " + token,
            },
        });

        Assert.Equal("alice", principal.SubjectId);
        Assert.Equal("tenant-1", principal.TenantId);
        Assert.True(principal.Roles.Has("operator"));
    }

    [Fact]
    public async Task Jwt_Auth_Invalid_Tenant_Claim_Returns_Anonymous()
    {
        var cfg = new AuthConfig { Mode = "jwt" };
        var token = DevIdp.Setup(cfg, new DevIdp.Options
        {
            Subject = "alice",
            Roles = new[] { "operator" },
            Tenant = "bad tenant",
        });

        var idp = new LocalIdentityProvider(cfg);
        var principal = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["Authorization"] = "Bearer " + token,
            },
        });

        Assert.Equal("anonymous", principal.SubjectId);
        Assert.Null(principal.TenantId);
        Assert.True(principal.Roles.Has("viewer"));
    }

    [Fact]
    public void ApiKeyStore_Tenant_Scoping()
    {
        var store = new ApiKeyStore();
        var (raw, record) = store.Create("k1", StringSet.Of("admin"), tenantId: "tenant-a");
        Assert.Equal("tenant-a", record.TenantId);
        Assert.NotNull(store.Validate(raw));

        var (raw2, record2) = store.Create("k2", StringSet.Of("admin"), tenantId: "tenant-b");
        Assert.Equal("tenant-b", record2.TenantId);
        Assert.Single(store.ListKeysForTenant("tenant-a"));
        Assert.Single(store.ListKeysForTenant("tenant-b"));

        Assert.True(store.RevokeForTenant(record.KeyId, "tenant-a"));
        Assert.Null(store.Validate(raw));
        Assert.True(store.RevokeForTenant(record.KeyId, "tenant-a"));
        Assert.NotNull(store.Validate(raw2));

        Assert.False(store.RevokeForTenant(record2.KeyId, "tenant-mismatch"));
        Assert.NotNull(store.Validate(raw2));
    }

    [Fact]
    public void ApiKeyStore_Requires_Valid_Tenant_On_Create()
    {
        var store = new ApiKeyStore();
        Assert.Throws<ArgumentException>(() => store.Create("bad", StringSet.Of("admin"), tenantId: "bad tenant"));
    }
}
