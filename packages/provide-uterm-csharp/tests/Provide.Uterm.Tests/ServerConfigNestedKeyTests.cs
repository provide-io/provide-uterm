//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// A typo inside a section is refused, not just one at the top level.
///
/// The reference gets this from one place: <c>extra="forbid"</c> sits on
/// <c>ServerBaseModel</c>, which every section model inherits, so
/// <c>[server] hsot = "…"</c> is refused exactly like a bad top-level key. This
/// port refused only the top level, so a misspelled key one line deeper was
/// still dropped in silence — and a section is where the security-relevant keys
/// live.
///
/// The key sets are the reference's field names, not this port's, for the reason
/// the top-level set already documents: one server.toml should be readable by
/// any port, so a section C# does not model is recognised and ignored rather
/// than refused.
///
/// **The drift test at the bottom is what makes this safe to have.** Hand-copied
/// field names rot, and a stale set would refuse a key the reference accepts —
/// which breaks a working deployment on upgrade, a worse failure than the one
/// being fixed. It ties every set to the recorded schema corpus instead.
/// </summary>
public sealed class ServerConfigNestedKeyTests
{
    private static string WriteToml(string body)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-nested-key-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, body);
        return path;
    }

    private static UtermServerConfig LoadToml(string body)
    {
        var path = WriteToml(body);
        try
        {
            return ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }
    }

    private static ArgumentException Refused(string body) =>
        Assert.Throws<ArgumentException>(() => LoadToml(body));

    /// <summary>
    /// The recorded schema corpus, found by ascending from the test binary.
    /// </summary>
    /// <remarks>
    /// Read live rather than copied into the output directory: the package
    /// Makefile runs `dotnet test --no-build`, so a copy step would not re-run
    /// and a regenerated corpus could be compared against a stale copy without
    /// anything looking wrong.
    /// </remarks>
    private static string LocateGolden()
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

    [Theory]
    [InlineData("[server]\nhsot = \"127.0.0.1\"", "hsot")]
    [InlineData("[server]\nprot = 8080", "prot")]
    [InlineData("[auth]\nmodez = \"jwt\"", "modez")]
    [InlineData("[auth]\nworker_bearer_tokne = \"x\"", "worker_bearer_tokne")]
    [InlineData("[recording]\nenabled_by_defualt = true", "enabled_by_defualt")]
    [InlineData("[security]\nunknown_knob = 1", "unknown_knob")]
    [InlineData("[control_plane]\nbaekend = \"memory\"", "baekend")]
    public void ATypoInsideASectionIsRefusedByName(string body, string offender)
    {
        var error = Refused(body);

        Assert.Contains(offender, error.Message, StringComparison.Ordinal);
        Assert.Contains("Extra inputs are not permitted", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void TheOffendingKeyIsNamedWithItsSection()
    {
        // `hsot` alone would send an operator hunting through the file. The
        // reference's own formatter drops the location, which is a shortcoming
        // worth not copying.
        var error = Refused("[server]\nhsot = \"127.0.0.1\"");

        Assert.Contains("server.hsot", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void AGoodNestedKeyIsStillAccepted()
    {
        var cfg = LoadToml("[server]\nhost = \"127.0.0.1\"\nport = 8123");

        Assert.Equal("127.0.0.1", cfg.Server.Host);
        Assert.Equal(8123, cfg.Server.Port);
    }

    [Fact]
    public void ASectionThisPortDoesNotModelIsStillReadable()
    {
        // `[profiles]` and `[webhooks]` exist in the reference and not in this
        // port's model. Refusing them would make a valid server.toml unusable
        // here, which is a worse divergence than not implementing the section.
        var cfg = LoadToml(
            "[profiles]\ndirectory = \"/tmp/uterm-profiles\"\n\n[webhooks]\nallow_loopback_destinations = false");

        Assert.NotNull(cfg);
    }

    [Fact]
    public void ATypoInsideASectionThisPortDoesNotModelIsStillRefused()
    {
        // Recognising the section does not mean abandoning its contents: the
        // reference validates `[profiles]` against its own model, so a typo
        // there is as refusable as one in `[server]`.
        var error = Refused("[profiles]\ndirectoryz = \"/tmp\"");

        Assert.Contains("directoryz", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void ASessionEntryKeepsItsOpenEndedOptions()
    {
        // `[[sessions]]` is the documented exception, both here and in the
        // reference: unrecognised keys fold into the connector's own config,
        // because a connector's options are open-ended by design.
        var cfg = LoadToml("""
            [[sessions]]
            session_id = "s1"
            connector_type = "telnet"
            host = "example.invalid"
            port = 23
            some_connector_knob = "value"
            """);

        var session = Assert.Single(cfg.Sessions);
        Assert.Equal("s1", session.SessionId);
        Assert.True(session.ConnectorConfig.ContainsKey("some_connector_knob"));
    }

    [Fact]
    public void AGraphicalTargetEntryIsValidatedLikeASection()
    {
        // `[[graphical_targets]]` is a list like sessions but has no open-ended
        // arm, so a typo in one is refused.
        var error = Refused("[[graphical_targets]]\ntarget_id = \"g1\"\nkidn = \"vnc\"");

        Assert.Contains("kidn", error.Message, StringComparison.Ordinal);
    }

    // -- The drift guard ---------------------------------------------------

    /// <summary>
    /// Every nested key set must match the reference's recorded field names.
    ///
    /// `configschema_golden.json` is generated from the reference's Pydantic
    /// models and drift-checked against them by `.ci/check_goldens.sh`, so it
    /// is the live surface rather than a second hand-maintained copy. If the
    /// reference gains a field and this port does not, this fails by name — the
    /// alternative is a stale set refusing a key the reference accepts, which
    /// breaks a working deployment on upgrade.
    ///
    /// <c>ConfigLoader.PortOnlyKeysForTests</c> is subtracted from our side
    /// first: a small, documented set of keys this port accepts with no
    /// reference-schema equivalent yet (see its own docstring), so a genuine
    /// port-specific extension does not read as drift.
    /// </summary>
    [Fact]
    public void TheKeySetsMatchTheReferencesRecordedSchema()
    {
        var path = LocateGolden();
        using var document = JsonDocument.Parse(File.ReadAllText(path));
        var specs = document.RootElement.GetProperty("specs");

        // An empty map would make the loop below iterate zero times and pass
        // vacuously — asserting a nonzero, known count is what keeps "nothing
        // was compared" from reading as "everything matched".
        Assert.Equal(ConfigLoader.KnownNestedKeysForTests.Count, ConfigLoader.SectionModelsForTests.Count);

        foreach (var (section, model) in ConfigLoader.SectionModelsForTests)
        {
            var recorded = specs.GetProperty(model)
                .EnumerateObject()
                .Select(property => property.Name)
                .OrderBy(name => name, StringComparer.Ordinal)
                .ToList();
            var portOnly = ConfigLoader.PortOnlyKeysForTests.GetValueOrDefault(section, Array.Empty<string>());
            var ours = ConfigLoader.KnownNestedKeysForTests[section]
                .Except(portOnly)
                .OrderBy(name => name, StringComparer.Ordinal)
                .ToList();

            Assert.Equal(recorded, ours);
        }
    }

    [Fact]
    public void EveryNestedSectionOfTheReferenceHasASet()
    {
        // A section with no set would silently accept anything inside it, which
        // is the behaviour this whole file replaces.
        var expected = new[]
        {
            "server", "auth", "control_plane", "ui", "recording", "profiles", "security",
            "tunnel", "webhooks", "pam", "governance", "audit", "graphical_targets", "sessions",
        };

        Assert.Equal(
            expected.OrderBy(name => name, StringComparer.Ordinal),
            ConfigLoader.KnownNestedKeysForTests.Keys.OrderBy(name => name, StringComparer.Ordinal));
    }
}
