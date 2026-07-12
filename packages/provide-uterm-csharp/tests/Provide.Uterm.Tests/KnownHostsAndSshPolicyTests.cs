//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Transports;

namespace Provide.Uterm.Tests.SshPolicy;

public class KnownHostsAndSshPolicyTests
{
    [Fact]
    public void Matches_PlainEntry_ByHostAndKey()
    {
        var key = Convert.FromBase64String("AAAAB3NzaC1yc2EAAAADAQABAAABgQC7"); // truncated-looking but any bytes
        // Use real base64 of our synthetic key bytes:
        key = new byte[] { 0x00, 0x00, 0x00, 0x07, 0x73, 0x73, 0x68, 0x2d, 0x72, 0x73, 0x61, 0x01, 0x02, 0x03 };
        var b64 = Convert.ToBase64String(key);
        var path = Path.Combine(Path.GetTempPath(), "kh-" + Guid.NewGuid().ToString("N"));
        File.WriteAllText(path, $"example.com ssh-rsa {b64}\n");
        try
        {
            Assert.True(KnownHosts.Matches("example.com", 22, "ssh-rsa", key, new[] { path }));
            Assert.False(KnownHosts.Matches("other.com", 22, "ssh-rsa", key, new[] { path }));
            Assert.False(KnownHosts.Matches("example.com", 22, "ssh-ed25519", key, new[] { path }));
            var wrong = (byte[])key.Clone();
            wrong[^1] ^= 0xff;
            Assert.False(KnownHosts.Matches("example.com", 22, "ssh-rsa", wrong, new[] { path }));
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void Matches_NonDefaultPort_BracketForm()
    {
        var key = new byte[] { 1, 2, 3, 4, 5 };
        var b64 = Convert.ToBase64String(key);
        var path = Path.Combine(Path.GetTempPath(), "kh-" + Guid.NewGuid().ToString("N"));
        File.WriteAllText(path, $"[host.example]:2222 ssh-ed25519 {b64}\n");
        try
        {
            Assert.True(KnownHosts.Matches("host.example", 2222, "ssh-ed25519", key, new[] { path }));
            Assert.False(KnownHosts.Matches("host.example", 22, "ssh-ed25519", key, new[] { path }));
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public async Task Connect_FailsClosed_WithoutKnownHostsOrInsecure()
    {
        var tr = new SshTransport();
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            tr.ConnectAsync("127.0.0.1", 22, new ConnectOptions
            {
                Ssh = new SshOptions { User = "u", Password = "p" },
            }));
        Assert.Contains("host key verification", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task Connect_FailsClosed_WhenKnownHostsFileMissing()
    {
        var tr = new SshTransport();
        var missing = Path.Combine(Path.GetTempPath(), "no-such-kh-" + Guid.NewGuid().ToString("N"));
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            tr.ConnectAsync("127.0.0.1", 22, new ConnectOptions
            {
                Ssh = new SshOptions
                {
                    User = "u",
                    Password = "p",
                    KnownHostsFiles = { missing },
                },
            }));
        Assert.Contains("no readable known_hosts", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task Connect_WithInsecure_AttemptsConnection()
    {
        // Closed port → connection error after host-key policy allows insecure.
        var tr = new SshTransport();
        var closed = FreeClosedPort();
        await Assert.ThrowsAnyAsync<Exception>(() =>
            tr.ConnectAsync("127.0.0.1", closed, new ConnectOptions
            {
                Ssh = new SshOptions
                {
                    User = "u",
                    Password = "p",
                    InsecureSkipHostKeyVerify = true,
                },
                Timeout = TimeSpan.FromMilliseconds(400),
            }));
    }

    [Fact]
    public void Matches_HandlesMarkersCommentsAndHashedHosts()
    {
        var key = new byte[] { 9, 8, 7, 6 };
        var b64 = Convert.ToBase64String(key);
        var path = Path.Combine(Path.GetTempPath(), "kh-" + Guid.NewGuid().ToString("N"));
        File.WriteAllText(path, string.Join('\n',
            "# comment",
            "",
            "@revoked",
            $"@cert-authority trusted.example ssh-ed25519 {b64}",
            $"|1|abc|def ssh-ed25519 {b64}",
            $"good.example,alias.example ssh-ed25519 {b64}",
            "short-line onlytwo"));
        try
        {
            Assert.True(KnownHosts.Matches("good.example", 22, "ssh-ed25519", key, new[] { path }));
            Assert.True(KnownHosts.Matches("alias.example", 22, "ssh-ed25519", key, new[] { path }));
            Assert.True(KnownHosts.Matches("trusted.example", 22, "ssh-ed25519", key, new[] { path }));
            Assert.False(KnownHosts.Matches("", 22, "ssh-ed25519", key, new[] { path }));
            Assert.False(KnownHosts.Matches("good.example", 22, "ssh-ed25519", key, Array.Empty<string>()));
            Assert.False(KnownHosts.Matches("good.example", 22, "ssh-ed25519", key, new[] { " ", path + "-missing" }));
            Assert.NotEmpty(KnownHosts.ExistingFiles(new[] { path, "/no/such" }));
            Assert.Contains("[x]:2222", KnownHosts.HostCandidates("x", 2222));
            // rsa-sha2 alias normalizes to ssh-rsa for type match when file says ssh-rsa
            var rsa = new byte[] { 1, 1, 1 };
            var rsaPath = Path.Combine(Path.GetTempPath(), "kh-rsa-" + Guid.NewGuid().ToString("N"));
            File.WriteAllText(rsaPath, $"h ssh-rsa {Convert.ToBase64String(rsa)}\n");
            try
            {
                Assert.True(KnownHosts.Matches("h", 22, "rsa-sha2-256", rsa, new[] { rsaPath }));
            }
            finally
            {
                File.Delete(rsaPath);
            }
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void Root_ProxySsh_RequiresUserAndKnownHosts()
    {
        var e = new StringWriter();
        var o = new StringWriter();
        var code = Provide.Uterm.Cli.Root.Execute(
            new[] { "proxy", "127.0.0.1", "22", "--transport", "ssh" }, o, e);
        Assert.Equal(1, code);
        Assert.Contains("ssh-user", e.ToString(), StringComparison.OrdinalIgnoreCase);

        e = new StringWriter();
        code = Provide.Uterm.Cli.Root.Execute(
            new[] { "proxy", "127.0.0.1", "22", "--transport", "ssh", "--ssh-user", "u" }, o, e);
        Assert.Equal(1, code);
        Assert.Contains("known-hosts", e.ToString(), StringComparison.OrdinalIgnoreCase);

        e = new StringWriter();
        o = new StringWriter();
        // --once with insecure should start and stop without connecting SSH
        var port = FreeClosedPort();
        code = Provide.Uterm.Cli.Root.Execute(
            new[]
            {
                "proxy", "127.0.0.1", "22", "--transport", "ssh", "--ssh-user", "u",
                "--insecure-ssh", "--port", port.ToString(), "--once", "--bind", "127.0.0.1",
            }, o, e);
        Assert.Equal(0, code);
        Assert.Contains("proxy ready", o.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    private static int FreeClosedPort()
    {
        var l = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        l.Start();
        var port = ((System.Net.IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }
}
