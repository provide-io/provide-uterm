//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

public sealed class ServerFanoutTests
{
    [Fact]
    public void Current_Authorization_Is_Rechecked_And_Group_Access_Does_Not_Override_It()
    {
        var cfg = UtermServerConfig.Default();
        var definition = new SessionDefinition
        {
            SessionId = "w1",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "alice",
        };
        var registry = new InMemorySessionRegistry([definition]);
        var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(),
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = registry,
        });
        var viewer = new Principal
        {
            SubjectId = "bob",
            Roles = StringSet.Of("viewer"),
            Scopes = StringSet.Of("*"),
        };

        var initial = server.AuthorizedFanoutMembers(viewer, ["w1", "missing"]);
        Assert.Equal(["w1"], initial.Allowed);
        Assert.Equal(["missing"], initial.Refused);

        definition.Visibility = "private";
        registry.Upsert(definition);
        var revoked = server.AuthorizedFanoutMembers(viewer, ["w1"]);
        Assert.Empty(revoked.Allowed);
        Assert.Equal(["w1"], revoked.Refused);
    }

    [Fact]
    public void Configuration_Defaults_Strict_And_Loads_Explicit_Permissive_Mode()
    {
        Assert.False(UtermServerConfig.Default().FanoutAllowUnknownMembers);
        var path = Path.Combine(Path.GetTempPath(), "uterm-fanout-" + Guid.NewGuid().ToString("N") + ".toml");
        try
        {
            File.WriteAllText(path, "fanout_allow_unknown_members = true\n");
            Assert.True(ConfigLoader.Load(path).FanoutAllowUnknownMembers);
        }
        finally
        {
            File.Delete(path);
        }
    }
}
