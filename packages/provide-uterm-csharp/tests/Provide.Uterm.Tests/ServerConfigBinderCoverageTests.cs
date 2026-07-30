//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Reflection;
using System.Text;
using System.Text.Json;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// Every key this port models must survive the trip from a TOML file into the
/// loaded config.
///
/// The loader keeps two independent lists: <c>KnownNestedKeys</c>, which decides
/// what a file is *allowed* to say, and <c>ApplyToml</c>, which decides what is
/// actually *read*. Nothing tied them together, so a key could sit in the
/// allowlist — passing validation, drift-checked against the reference, looking
/// entirely wired — while the binder never touched it. That is what happened to
/// all of <c>[ui]</c>, <c>[recording]</c> and <c>[tunnel]</c>: eighteen keys
/// accepted without complaint and silently discarded, including
/// <c>tunnel.cookie_secure</c> and <c>tunnel.ip_binding</c>.
///
/// The existing test that looks like it covered this is
/// <c>ServerConfigUnknownKeyTests.The_Whole_Reference_Key_Surface_Still_Loads</c>,
/// which writes <c>[ui] app_path</c>, <c>[recording] enabled_by_default</c> and
/// <c>[tunnel] token_ttl_s</c> into a file and then asserts nothing about any of
/// them. It proves the loader does not throw. It cannot notice that the values
/// went nowhere.
///
/// So this test asserts the one thing that matters and cannot be faked: writing
/// a key changes the loaded config. It is generated from
/// <c>configschema_golden.json</c> and from reflection over this port's own
/// models, with no hand-maintained list of keys to fall out of date — a key is
/// in scope precisely when C# declares a property for it, and out of scope when
/// it does not (a parity gap, tracked separately, not a binding bug).
/// </summary>
public sealed class ServerConfigBinderCoverageTests
{
    /// <summary>A value written into a probe file, chosen to collide with no default.</summary>
    private const string ProbeString = "uterm-binder-probe";

    private const int ProbeInt = 4242;
    private const double ProbeFloat = 33.5;

    /// <summary>
    /// Section name in TOML → the golden's model name and this port's section object.
    /// </summary>
    /// <remarks>
    /// The two table-array sections (<c>[[sessions]]</c>,
    /// <c>[[graphical_targets]]</c>) are covered by
    /// <see cref="ATableArraySectionBindsEveryKeyItModels"/> instead, because a
    /// probe for them has to write an array entry rather than a table.
    /// </remarks>
    private static readonly (string Toml, string Model)[] ScalarSections =
    {
        ("server", "ServerBindConfig"),
        ("auth", "AuthConfig"),
        ("ui", "UiConfig"),
        ("recording", "RecordingConfig"),
        ("control_plane", "ControlPlaneConfig"),
        ("security", "SecurityConfig"),
        ("tunnel", "TunnelConfig"),
        ("governance", "GovernanceConfig"),
        ("audit", "AuditConfig"),
        ("webhooks", "WebhooksConfig"),
        ("profiles", "ProfileStoreConfig"),
        ("pam", "PamConfig"),
    };

    /// <summary>
    /// Keys whose prerequisite must be written alongside them, or a cross-field
    /// validator refuses the file before the binder is reached.
    /// </summary>
    private static readonly Dictionary<string, string> Prerequisites = new(StringComparer.Ordinal)
    {
        // require_upstream_proxy_secret=true demands a non-empty secret.
        ["auth.require_upstream_proxy_secret"] = "upstream_proxy_secret = \"probe-secret\"",
    };

    private static string GoldenPath()
    {
        var parts = new[] { "packages", "provide-uterm-ts", "testdata", "configschema_golden.json" };
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = Path.Combine(new[] { dir.FullName }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        throw new FileNotFoundException("configschema_golden.json not found above " + AppContext.BaseDirectory);
    }

    /// <summary>Compare names across the snake_case/PascalCase boundary.</summary>
    /// <remarks>
    /// Dropping the underscores rather than mapping word-for-word, because the
    /// boundaries do not always agree: the reference writes
    /// <c>fitaddon_cdn</c> where this port writes <c>FitAddonCdn</c>.
    /// </remarks>
    private static string Flatten(string name) => name.Replace("_", "", StringComparison.Ordinal).ToLowerInvariant();

    private static PropertyInfo? PropertyFor(Type section, string key) =>
        section.GetProperties(BindingFlags.Public | BindingFlags.Instance)
            .FirstOrDefault(property => Flatten(property.Name) == Flatten(key));

    /// <summary>
    /// This port's object for a TOML section, or null when it models none.
    /// </summary>
    /// <remarks>
    /// A whole section may be absent — <c>[pam]</c>, <c>[audit]</c>,
    /// <c>[webhooks]</c> and <c>[profiles]</c> all are. That is a parity gap
    /// rather than a binding bug, and the allowlist deliberately still accepts
    /// those sections so one server.toml stays readable by every port. Skipped
    /// here for the same reason an unmodelled key is skipped.
    /// </remarks>
    private static object? SectionObject(UtermServerConfig cfg, string tomlSection)
    {
        var property = typeof(UtermServerConfig)
            .GetProperties(BindingFlags.Public | BindingFlags.Instance)
            .FirstOrDefault(candidate => Flatten(candidate.Name) == Flatten(tomlSection));
        return property?.GetValue(cfg);
    }

    /// <summary>
    /// Every TOML spelling worth trying for one key — enough that at least one of
    /// them must differ from whatever this port's default happens to be.
    /// </summary>
    /// <remarks>
    /// More than one candidate per key on purpose: a probe that happened to write
    /// the existing default would leave the config unchanged and read exactly
    /// like an unbound key. Booleans get both values, literals get every choice,
    /// so "no candidate changed anything" is proof of a missing binding rather
    /// than an unlucky guess.
    /// </remarks>
    private static IEnumerable<string> Candidates(JsonElement spec)
    {
        var kind = spec.GetProperty("kind").GetString();
        switch (kind)
        {
            case "bool":
                yield return "true";
                yield return "false";
                break;
            case "int":
                yield return ProbeInt.ToString(System.Globalization.CultureInfo.InvariantCulture);
                break;
            case "float":
                yield return ProbeFloat.ToString(System.Globalization.CultureInfo.InvariantCulture);
                break;
            case "literal":
                foreach (var choice in spec.GetProperty("choices").EnumerateArray())
                {
                    yield return "\"" + choice.GetString() + "\"";
                }

                break;
            case "list":
                yield return "[\"" + ProbeString + "\"]";
                break;
            case "dict":
                yield return "{ probe = \"" + ProbeString + "\" }";
                break;
            case "datetime":
                yield return "\"2026-07-30T00:00:00Z\"";
                break;
            default:
                // str and path.
                yield return "\"" + ProbeString + "\"";
                break;
        }
    }

    private static UtermServerConfig LoadToml(string body)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-binder-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, body);
        try
        {
            return ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }
    }

    /// <summary>Serialize enough of the config to see any single key land.</summary>
    private static string Fingerprint(UtermServerConfig cfg) =>
        JsonSerializer.Serialize(cfg, new JsonSerializerOptions { WriteIndented = false });

    [Fact]
    public void EveryModelledNestedKeyReachesTheLoadedConfig()
    {
        using var document = JsonDocument.Parse(File.ReadAllText(GoldenPath()));
        var specs = document.RootElement.GetProperty("specs");
        var baseline = Fingerprint(LoadToml(""));
        var unbound = new List<string>();
        var checkedCount = 0;

        foreach (var (tomlSection, modelName) in ScalarSections)
        {
            if (SectionObject(UtermServerConfig.Default(), tomlSection) is not { } sectionObject)
            {
                continue;
            }

            var sectionType = sectionObject.GetType();

            foreach (var field in specs.GetProperty(modelName).EnumerateObject())
            {
                // Out of scope when this port declares no property for the key:
                // that is a parity gap, not a binder that forgot one.
                if (PropertyFor(sectionType, field.Name) is null)
                {
                    continue;
                }

                checkedCount++;
                var qualified = tomlSection + "." + field.Name;
                var landed = false;

                foreach (var candidate in Candidates(field.Value))
                {
                    var body = new StringBuilder()
                        .Append('[').Append(tomlSection).AppendLine("]")
                        .Append(field.Name).Append(" = ").AppendLine(candidate);
                    if (Prerequisites.TryGetValue(qualified, out var prerequisite))
                    {
                        body.AppendLine(prerequisite);
                    }

                    // A validator that refuses the value proves the key was read,
                    // which is all this test claims. Anything else is a genuine
                    // failure and must not be swallowed.
                    try
                    {
                        if (Fingerprint(LoadToml(body.ToString())) != baseline)
                        {
                            landed = true;
                            break;
                        }
                    }
                    catch (ArgumentException)
                    {
                        landed = true;
                        break;
                    }
                }

                if (!landed)
                {
                    unbound.Add(qualified);
                }
            }
        }

        Assert.True(checkedCount > 0, "the probe generated no cases — the golden or the reflection broke");
        Assert.True(
            unbound.Count == 0,
            $"{unbound.Count} modelled key(s) are accepted by the loader and then discarded:\n  "
                + string.Join("\n  ", unbound));
    }

    [Fact]
    public void ATableArraySectionBindsEveryKeyItModels()
    {
        using var document = JsonDocument.Parse(File.ReadAllText(GoldenPath()));
        var specs = document.RootElement.GetProperty("specs");
        var unbound = new List<string>();

        foreach (var (tomlSection, modelName, sectionType) in new[]
                 {
                     ("sessions", "SessionDefinition", typeof(SessionDefinition)),
                     ("graphical_targets", "GraphicalTargetConfig", typeof(GraphicalTargetDefinition)),
                 })
        {
            // A single entry with only the keys it cannot load without is the
            // baseline; each probe adds one more key, so a diff can only come from
            // that key. The identity keys are written from a map rather than a
            // string so a probe for one of them can replace its own line instead
            // of colliding with it — TOML refuses a redefined key outright.
            var identity = tomlSection == "sessions"
                ? new Dictionary<string, string>(StringComparer.Ordinal)
                {
                    ["session_id"] = "\"probe\"",
                    ["connector_type"] = "\"shell\"",
                }
                : new Dictionary<string, string>(StringComparer.Ordinal)
                {
                    ["target_id"] = "\"probe\"",
                    ["protocol"] = "\"rfb\"",
                    ["target_address"] = "\"127.0.0.1:5900\"",
                };

            string Entry(string? probeKey, string? probeValue)
            {
                var body = new StringBuilder().Append("[[").Append(tomlSection).AppendLine("]]");
                foreach (var (key, value) in identity)
                {
                    body.Append(key).Append(" = ").AppendLine(key == probeKey ? probeValue : value);
                }

                if (probeKey is not null && !identity.ContainsKey(probeKey))
                {
                    body.Append(probeKey).Append(" = ").AppendLine(probeValue);
                }

                return body.ToString();
            }

            var baseline = Fingerprint(LoadToml(Entry(null, null)));

            foreach (var field in specs.GetProperty(modelName).EnumerateObject())
            {
                if (PropertyFor(sectionType, field.Name) is null)
                {
                    continue;
                }

                var qualified = tomlSection + "[]." + field.Name;
                var landed = false;

                foreach (var candidate in Candidates(field.Value))
                {
                    try
                    {
                        if (Fingerprint(LoadToml(Entry(field.Name, candidate))) != baseline)
                        {
                            landed = true;
                            break;
                        }
                    }
                    catch (ArgumentException)
                    {
                        landed = true;
                        break;
                    }
                }

                if (!landed)
                {
                    unbound.Add(qualified);
                }
            }
        }

        Assert.True(
            unbound.Count == 0,
            $"{unbound.Count} modelled entry key(s) are accepted and then discarded:\n  "
                + string.Join("\n  ", unbound));
    }
}
