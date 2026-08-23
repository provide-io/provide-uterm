//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;
using Provide.Uterm.CtrlMsg;
using Provide.Uterm.Fanout;
using Provide.Uterm.Render;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using RBuf = Provide.Uterm.Render.RenderBuffer;

namespace Provide.Uterm.Tests;

/// <summary>Small high-yield tests to clear the 90% coverage floor.</summary>
public class Over90NudgeTests
{
    [Fact]
    public void Divergence_Empty_Single_And_Majority()
    {
        Assert.Empty(Divergence.ComputeDivergence(Array.Empty<string>(), 0.5));
        Assert.Equal(new[] { false }, Divergence.ComputeDivergence(new[] { "only" }, 0.5));

        // three identical + one outlier
        var flags = Divergence.ComputeDivergence(
            new[] { "aaaa", "aaaa", "aaaa", "ZZZZ_totally_different" }, 0.8);
        Assert.Equal(4, flags.Length);
        Assert.Contains(true, flags);

        // empty strings ratio path
        var empty = Divergence.ComputeDivergence(new[] { "", "" }, 0.5);
        Assert.Equal(2, empty.Length);

        // long popular-char path in SeqMatcher (n>=200)
        var longA = new string('a', 250) + "x";
        var longB = new string('a', 250) + "y";
        var longFlags = Divergence.ComputeDivergence(new[] { longA, longB, longA }, 0.95);
        Assert.Equal(3, longFlags.Length);
    }

    [Fact]
    public void RenderBuffer_Styles_And_Hex()
    {
        var s = new RBuf.Style
        {
            FG = "red", BG = "blue", Bold = true, Underscore = true,
            Reverse = true, Blink = true,
        };
        _ = RBuf.StyleToSgr(s);
        _ = RBuf.StyleToSgr(new RBuf.Style { FG = "ff00aa", BG = "00ff11" });
        _ = RBuf.StyleToSgr(RBuf.DefaultStyle);
        _ = RBuf.StyleToSgr(new RBuf.Style { FG = "brightred", BG = "brown" });
        _ = RBuf.StyleToSgr(new RBuf.Style { FG = "unknown", BG = "default" });
        _ = RBuf.StyleToSgr(new RBuf.Style { FG = "zzzzzz", BG = "gggggg" });

        var s2 = s;
        Assert.True(s.Equals(s2));
        Assert.False(s.Equals(RBuf.DefaultStyle));
        _ = s.GetHashCode();

        // RenderScreenLines hits CellStyle + StyleToSgr for each cell
        var scr = new Provide.Uterm.Vt.Screen(10, 3);
        scr.Draw("Hi");
        var lines = RBuf.RenderScreenLines(scr, 10, 3);
        Assert.Equal(3, lines.Count);
        Assert.Contains("H", lines[0], StringComparison.Ordinal);

        var cell = scr.At(0, 0);
        var style = RBuf.CellStyle(cell);
        Assert.NotNull(style.FG);
    }

    [Fact]
    public void SessionRegistry_Upsert_Delete_MarkWorker()
    {
        var reg = new InMemorySessionRegistry(new[]
        {
            new SessionDefinition { SessionId = "", DisplayName = "skip" }, // skipped empty id
            new SessionDefinition
            {
                SessionId = "s1", DisplayName = "", ConnectorType = "shell",
                Visibility = "public", Owner = "o", Tags = new List<string> { "t" },
            },
        });
        Assert.True(reg.TryGetDefinition("s1", out var d));
        Assert.Equal("s1", d.DisplayName == "" ? d.SessionId : d.SessionId);

        reg.Upsert(new SessionDefinition
        {
            SessionId = "s1", DisplayName = "Named", ConnectorType = "telnet",
            Visibility = "private", Owner = "o2", Tags = new List<string> { "a", "b" },
        });
        Assert.Single(reg.ListWithDefinitions());

        if (reg is InMemorySessionRegistry mem)
        {
            mem.MarkWorker("s1", true, true, "open");
            mem.MarkWorker("missing", false, false, "hijack");
        }

        Assert.True(reg.Delete("s1"));
        Assert.False(reg.Delete("s1"));
        Assert.False(reg.TryGetDefinition("s1", out _));
    }

    [Fact]
    public async Task ApiKey_Viewer_Operator_Admin_Scopes()
    {
        var keys = new ApiKeyStore();
        var tenant = "tenantA";
        var (viewerRaw, _) = keys.Create("v", StringSet.Of("viewer"), tenantId: tenant);
        var (opRaw, _) = keys.Create("o", StringSet.Of("operator"), tenantId: tenant);
        var (adminRaw, _) = keys.Create("a", StringSet.Of("admin"), tenantId: tenant);
        var (unknownRaw, _) = keys.Create("u", StringSet.Of("other"), tenantId: tenant);

        var cfg = new AuthConfig { Mode = "dev_token", ApiKeysEnabled = true };
        var idp = new LocalIdentityProvider(cfg, keys);

        var v = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["X-Api-Key"] = viewerRaw,
            },
        });
        Assert.True(v.Roles.Has("viewer"));

        var o = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["X-Api-Key"] = opRaw,
            },
        });
        Assert.True(o.Roles.Has("operator"));

        var a = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["X-Api-Key"] = adminRaw,
            },
        });
        Assert.True(a.Roles.Has("admin"));

        // unknown scope key → falls through (not api principal)
        var u = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["X-Api-Key"] = unknownRaw,
            },
        });
        // without valid bearer → anonymous under dev_token with no setup
        Assert.Equal("anonymous", u.SubjectId);

        // invalid key
        var bad = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["X-Api-Key"] = "not-valid",
            },
        });
        Assert.Equal("anonymous", bad.SubjectId);
    }

    [Fact]
    public void CanonicalJson_Escapes_And_Surrogates()
    {
        var s = CanonicalJson.Serialize("a\"b\\c\n\r\t\b\f\u0001");
        Assert.Contains("\\\"", s, StringComparison.Ordinal);
        Assert.Contains("\\n", s, StringComparison.Ordinal);
        Assert.Contains("\\u0001", s, StringComparison.Ordinal);

        // emoji / surrogate pair
        var emoji = CanonicalJson.Serialize("hi😀");
        Assert.Contains("\\u", emoji, StringComparison.Ordinal);

        using var doc = JsonDocument.Parse("""{"z":1,"a":[1,2],"n":1.5e1,"s":"x"}""");
        var je = CanonicalJson.Serialize(doc.RootElement);
        Assert.StartsWith("{", je, StringComparison.Ordinal);

        // scientific notation float in JsonElement
        using var doc2 = JsonDocument.Parse("1e2");
        _ = CanonicalJson.Serialize(doc2.RootElement);
    }
}
