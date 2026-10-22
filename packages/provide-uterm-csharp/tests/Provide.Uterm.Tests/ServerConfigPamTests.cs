//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// <c>[pam] capture_socket_mode</c> loads as the reference loads it: this port
/// binds no capture socket, but it accepts a config that names 0o600 or 0o660
/// and refuses any other mode, or a value that is not an integer, with the
/// reference's own text.
/// </summary>
public sealed class ServerConfigPamTests
{
    private static UtermServerConfig Load(string body)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-pam-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, "[pam]\n" + body + "\n");
        try
        {
            return ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Theory]
    [InlineData("capture_socket_mode = 0o600")]
    [InlineData("capture_socket_mode = 0o660")]
    [InlineData("capture_socket_mode = 432")]
    [InlineData("mode = \"capture\"")]
    public void OwnerOnlyAndOwnerAndGroupAreAccepted(string body)
    {
        Assert.NotNull(Load(body));
    }

    [Theory]
    [InlineData("0o666", "0o666")]
    [InlineData("0o664", "0o664")]
    [InlineData("0o640", "0o640")]
    [InlineData("0o700", "0o700")]
    [InlineData("0o770", "0o770")]
    [InlineData("0o2660", "0o2660")]
    [InlineData("0", "0o0")]
    [InlineData("-1", "-0o1")]
    [InlineData("-384", "-0o600")]
    [InlineData("-9223372036854775808", "-0o1000000000000000000000")]
    [InlineData("9223372036854775807", "0o777777777777777777777")]
    public void AnyOtherModeIsRefusedQuotingItAsPythonsOctDoes(string written, string shown)
    {
        var error = Assert.Throws<ArgumentException>(() => Load("capture_socket_mode = " + written));
        Assert.Equal("pam.capture_socket_mode must be 0o600 or 0o660, got: " + shown, error.Message);
    }

    [Theory]
    [InlineData("\"0o660\"")]
    [InlineData("\"432\"")]
    [InlineData("432.0")]
    [InlineData("true")]
    [InlineData("[432]")]
    public void AModeThatIsNotAnIntegerIsRefused(string written)
    {
        var error = Assert.Throws<ArgumentException>(() => Load("capture_socket_mode = " + written));
        Assert.Equal("pam.capture_socket_mode must be an integer: 0o600 or 0o660", error.Message);
    }
}
