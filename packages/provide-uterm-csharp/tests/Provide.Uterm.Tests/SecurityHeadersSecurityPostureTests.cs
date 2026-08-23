// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

public sealed class SecurityHeadersSecurityPostureTests
{
    private static JsonDocument Golden()
    {
        var local = TestData.PathTo("securityheaders_golden.json");
        if (File.Exists(local))
        {
            return JsonDocument.Parse(File.ReadAllText(local));
        }

        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, "packages", "provide-uterm-ts", "testdata", "securityheaders_golden.json");
            if (File.Exists(candidate))
            {
                return JsonDocument.Parse(File.ReadAllText(candidate));
            }

            dir = dir.Parent;
        }

        throw new FileNotFoundException("securityheaders_golden.json not found above " + AppContext.BaseDirectory);
    }

    private static string? OrNull(JsonElement value) =>
        value.ValueKind == JsonValueKind.Null ? null : value.GetString();

    private static SecurityConfig AsConfig(JsonElement testcase)
    {
        var config = new SecurityConfig { Mode = testcase.GetProperty("mode").GetString() ?? "" };
        var overrides = testcase.GetProperty("overrides");
        foreach (var kv in overrides.EnumerateObject())
        {
            var value = OrNull(kv.Value);
            switch (kv.Name)
            {
                case "csp":
                    config.Csp = value;
                    break;
                case "hsts":
                    config.Hsts = value;
                    break;
                case "x_frame_options":
                    config.XFrameOptions = value;
                    break;
                case "x_content_type_options":
                    config.XContentTypeOptions = value;
                    break;
                case "referrer_policy":
                    config.ReferrerPolicy = value;
                    break;
                case "permissions_policy":
                    config.PermissionsPolicy = value;
                    break;
                default:
                    Assert.Fail("unexpected override key in security headers golden: " + kv.Name);
                    break;
            }
        }

        return config;
    }

    private static IReadOnlyList<(string Header, string Value)> AsPairs(JsonElement headers) =>
        headers.EnumerateArray()
            .Select(item => (item[0].GetString() ?? string.Empty, item[1].GetString() ?? string.Empty))
            .ToList();

    [Fact]
    public void SecurityHeaders_Resolve_Tracks_GoldenRecord()
    {
        using var document = Golden();
        var root = document.RootElement;
        var cases = root.GetProperty("resolved");
        var expectedOrder = root.GetProperty("header_order").EnumerateArray()
            .Select(v => v.GetString() ?? string.Empty)
            .ToList();
        Assert.Equal(expectedOrder, SecurityHeaders.ResolveSecurityHeaders(new SecurityConfig { Mode = "strict" })
            .Select(pair => pair.Header).ToList());

        foreach (var testcase in cases.EnumerateArray())
        {
            var config = AsConfig(testcase);
            var expected = AsPairs(testcase.GetProperty("headers"));
            var actual = SecurityHeaders.ResolveSecurityHeaders(config).Select(p => (p.Header, p.Value)).ToList();
            Assert.Equal(expected, actual);
        }
    }

    [Fact]
    public void SecurityHeaders_Resolve_TwoKnownModes_MatchGoldenDefaults()
    {
        using var document = Golden();
        var root = document.RootElement;
        Assert.Equal("strict", root.GetProperty("default_mode").GetString());
        var schemaDefault = AsPairs(root.GetProperty("schema_default"));
        Assert.Equal(schemaDefault, SecurityHeaders.ResolveSecurityHeaders(new SecurityConfig { Mode = root.GetProperty("default_mode").GetString()! })
            .Select(pair => (pair.Header, pair.Value)).ToList());
    }
}
