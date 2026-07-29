//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.IdentityModel.Tokens.Jwt;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>What the dev-token IdP actually puts in the token it mints.</summary>
public sealed class ServerAuthDevTokenClaimsTests
{
    private static JwtSecurityToken Mint()
    {
        var auth = new AuthConfig { Mode = "dev_token" };
        var jwt = DevIdp.Setup(auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "devtok-" + Guid.NewGuid().ToString("N")),
            Subject = "dev-user",
            Roles = ["admin"],
        });
        return new JwtSecurityTokenHandler().ReadJwtToken(jwt);
    }

    [Fact]
    public void Dev_Token_Names_Its_Audience_Once()
    {
        // JwtSecurityToken writes the audience it was constructed with; adding
        // the same claim by hand as well makes `aud` an array of one value
        // repeated — verifiable either way, but wrong to anything reading it.
        Assert.Equal(["provide-uterm-server"], Mint().Audiences.ToArray());
    }

    [Fact]
    public void Dev_Token_Names_Its_Issuer_Once()
    {
        var token = Mint();

        Assert.Equal("provide-uterm", token.Issuer);
        Assert.Single(token.Claims, claim => claim.Type == JwtRegisteredClaimNames.Iss);
    }

    [Fact]
    public void Dev_Token_Still_Carries_Subject_And_Roles()
    {
        var token = Mint();

        Assert.Equal("dev-user", token.Claims.Single(claim => claim.Type == "sub").Value);
        Assert.Equal("admin", token.Claims.Single(claim => claim.Type == "roles").Value);
    }
}
