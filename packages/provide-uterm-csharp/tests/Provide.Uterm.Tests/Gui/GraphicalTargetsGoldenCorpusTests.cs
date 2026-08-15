//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Server;
using Def = Provide.Uterm.Server.GraphicalTargetDefinition;

namespace Provide.Uterm.Tests.Gui;

/// <summary>
/// The C# port driven by the shared graphical-target golden corpus.
///
/// The corpus is read IN PLACE from
/// <c>packages/provide-uterm-ts/testdata/graphicaltargets_golden.json</c> — the
/// same bytes the TypeScript port loads
/// (<c>packages/provide-uterm-ts/src/graphical/targets.test.ts</c>) and the same
/// bytes <c>gen_graphicaltargets_golden.py</c> writes. It is deliberately NOT
/// copied under this package's <c>testdata/</c>: a copy would be a twinned
/// fixture, would need registering in <c>scripts/check_protocol_drift.py</c>,
/// and would then be one more thing that can silently drift. One file, three
/// runtimes.
///
/// Why this exists: C# was the one port that never executed the corpus, which
/// is how a missing <c>Close()</c> — and with it an unreachable
/// <c>GraphicalTargetErrorCode.Closed</c> — survived behind a suite at 97%+
/// line coverage. Hand-written tests assert what their author thought of; this
/// asserts what the reference actually did.
///
/// Runs in the ~Gui gate batch (namespace <c>…Tests.Gui</c>).
/// </summary>
public class GraphicalTargetsGoldenCorpusTests
{
    /// <summary>The instant the corpus was recorded at (the generator's WHEN).</summary>
    private static readonly DateTimeOffset When = new(2026, 1, 2, 3, 4, 5, TimeSpan.Zero);

    private static readonly JsonDocument Corpus = LoadCorpus();

    private static JsonElement Golden => Corpus.RootElement;

    /// <summary>
    /// Walk up from the test assembly to the repository root and read the
    /// TypeScript package's corpus where it lives.
    /// </summary>
    private static JsonDocument LoadCorpus()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(
                dir.FullName, "packages", "provide-uterm-ts", "testdata", "graphicaltargets_golden.json");
            if (File.Exists(candidate))
            {
                return JsonDocument.Parse(File.ReadAllText(candidate));
            }

            dir = dir.Parent;
        }

        throw new FileNotFoundException(
            "packages/provide-uterm-ts/testdata/graphicaltargets_golden.json not found above " +
            AppContext.BaseDirectory);
    }

    // --- corpus → CLR ------------------------------------------------------

    /// <summary>A JSON value as the plain CLR shapes the wire builder produces.</summary>
    private static object? JsonToClr(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.String => value.GetString(),
        JsonValueKind.Number => value.TryGetInt64(out var i) ? i : value.GetDouble(),
        JsonValueKind.True => true,
        JsonValueKind.False => false,
        JsonValueKind.Null => null,
        JsonValueKind.Object => value.EnumerateObject()
            .ToDictionary(p => p.Name, p => JsonToClr(p.Value), StringComparer.Ordinal),
        JsonValueKind.Array => value.EnumerateArray().Select(JsonToClr).ToList(),
        _ => throw new InvalidOperationException($"unsupported corpus value kind {value.ValueKind}"),
    };

    /// <summary>A stable rendering of a value, so a mismatch reads as a diff.</summary>
    private static string Canon(object? value) => value switch
    {
        null => "null",
        string s => JsonSerializer.Serialize(s),
        bool b => b ? "true" : "false",
        IDictionary<string, object?> d =>
            "{" + string.Join(",", d.OrderBy(p => p.Key, StringComparer.Ordinal)
                .Select(p => JsonSerializer.Serialize(p.Key) + ":" + Canon(p.Value))) + "}",
        System.Collections.IEnumerable e and not string =>
            "[" + string.Join(",", e.Cast<object?>().Select(Canon)) + "]",
        IFormattable f => f.ToString(null, CultureInfo.InvariantCulture),
        _ => throw new InvalidOperationException($"unsupported wire value type {value.GetType()}"),
    };

    /// <summary>The corpus's snake_case fields as a definition.</summary>
    private static Def TargetFrom(JsonElement fields)
    {
        var target = new Def { CreatedAt = When };
        foreach (var field in fields.EnumerateObject())
        {
            switch (field.Name)
            {
                case "target_id": target.TargetId = field.Value.GetString()!; break;
                case "tenant_id": target.TenantId = field.Value.GetString()!; break;
                case "display_name": target.DisplayName = field.Value.GetString()!; break;
                case "protocol": target.Protocol = field.Value.GetString()!; break;
                case "endpoint": target.Endpoint = field.Value.GetString(); break;
                case "secret": target.Secret = field.Value.GetString(); break;
                case "width": target.Width = field.Value.GetInt32(); break;
                case "height": target.Height = field.Value.GetInt32(); break;
                case "is_system": target.IsSystem = field.Value.GetBoolean(); break;
                case "is_static": target.IsStatic = field.Value.GetBoolean(); break;
                case "ca_secret_ref": target.CaSecretRef = field.Value.GetString(); break;
                case "client_cert_secret_ref": target.ClientCertSecretRef = field.Value.GetString(); break;
                case "client_key_secret_ref": target.ClientKeySecretRef = field.Value.GetString(); break;
                case "created_by": target.CreatedBy = field.Value.GetString(); break;
                case "updated_by": target.UpdatedBy = field.Value.GetString(); break;
                case "updated_at":
                    target.UpdatedAt = DateTimeOffset.Parse(
                        field.Value.GetString()!, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind);
                    break;
                case "config":
                    target.Config = (Dictionary<string, object?>)JsonToClr(field.Value)!;
                    break;
                default: throw new InvalidOperationException($"unknown corpus field {field.Name}");
            }
        }

        return target;
    }

    /// <summary>
    /// The reference's <c>to_wire_dict</c>: snake_case, null optionals omitted,
    /// an empty settings map omitted.
    ///
    /// The two instants are left as <see cref="DateTimeOffset"/> rather than
    /// rendered — the port has no wire-text writer of its own, so a rendering
    /// here would be this test asserting against itself. Their values are
    /// checked by <see cref="Ledger"/>.
    /// </summary>
    private static Dictionary<string, object?> ToWire(Def target)
    {
        var wire = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["target_id"] = target.TargetId,
            ["tenant_id"] = target.TenantId,
            ["display_name"] = target.DisplayName,
            ["protocol"] = target.Protocol,
            ["width"] = (long)target.Width,
            ["height"] = (long)target.Height,
            ["is_system"] = target.IsSystem,
            ["is_static"] = target.IsStatic,
            ["created_at"] = target.CreatedAt,
        };
        if (target.Endpoint is not null) { wire["endpoint"] = target.Endpoint; }
        if (target.Secret is not null) { wire["secret"] = target.Secret; }
        if (target.CaSecretRef is not null) { wire["ca_secret_ref"] = target.CaSecretRef; }
        if (target.ClientCertSecretRef is not null) { wire["client_cert_secret_ref"] = target.ClientCertSecretRef; }
        if (target.ClientKeySecretRef is not null) { wire["client_key_secret_ref"] = target.ClientKeySecretRef; }
        if (target.CreatedBy is not null) { wire["created_by"] = target.CreatedBy; }
        if (target.UpdatedBy is not null) { wire["updated_by"] = target.UpdatedBy; }
        if (target.UpdatedAt is not null) { wire["updated_at"] = target.UpdatedAt.Value; }
        if (target.Config.Count > 0) { wire["config"] = new Dictionary<string, object?>(target.Config, StringComparer.Ordinal); }
        return wire;
    }

    /// <summary>The corpus's error name for an error code (<c>ALREADY_EXISTS</c>).</summary>
    private static string Screaming(string name)
    {
        var text = new StringBuilder();
        foreach (var c in name)
        {
            if (char.IsUpper(c) && text.Length > 0)
            {
                text.Append('_');
            }

            text.Append(char.ToUpperInvariant(c));
        }

        return text.ToString();
    }

    // --- outcomes ----------------------------------------------------------

    private sealed class Outcome
    {
        public object? Value { get; init; }
        public string? Error { get; init; }
        public string? Message { get; init; }
    }

    private static async Task<Outcome> RunAsync(Func<Task<object?>> call)
    {
        try
        {
            return new Outcome { Value = await call() };
        }
        catch (GraphicalTargetException ex)
        {
            return new Outcome { Error = Screaming(ex.Code.ToString()), Message = ex.Message };
        }
        catch (InvalidOperationException ex)
        {
            // Not a coded refusal, and not a name the corpus can ever contain,
            // so this cannot make a case pass — it turns a divergence that
            // would otherwise surface as a raw stack trace into a readable
            // "expected CONFLICT, got InvalidOperationException" diff.
            return new Outcome { Error = nameof(InvalidOperationException), Message = ex.Message };
        }
    }

    /// <summary>
    /// Assert a call matched what the reference did, value or refusal.
    /// </summary>
    private static async Task ExpectOutcomeAsync(JsonElement recorded, Ledger ledger, Func<Task<object?>> call)
    {
        var actual = await RunAsync(call);
        if (recorded.TryGetProperty("error", out var expectedError))
        {
            Assert.Equal(expectedError.GetString(), actual.Error);
            Assert.Equal(recorded.GetProperty("message").GetString(), actual.Message);
            return;
        }

        Assert.Null(actual.Error);
        AssertValue(recorded.GetProperty("value"), actual.Value, ledger);
    }

    /// <summary>A recorded value against what the port produced, instants aside.</summary>
    private static void AssertValue(JsonElement expected, object? actual, Ledger ledger)
    {
        if (expected.ValueKind == JsonValueKind.Null)
        {
            Assert.Null(actual);
            return;
        }

        // A listing: every row is a wire document, so every row goes through
        // the instant-aware comparison rather than a flat rendering.
        if (actual is IReadOnlyList<Dictionary<string, object?>> rows)
        {
            var wanted = expected.EnumerateArray().ToList();
            Assert.Equal(wanted.Count, rows.Count);
            for (var i = 0; i < wanted.Count; i++)
            {
                AssertWire(wanted[i], rows[i], ledger);
            }

            return;
        }

        // Everything else a registry step records is one wire document. A
        // corpus that grew a value of some other shape must say so here rather
        // than be compared as whatever this happened to do with it.
        Assert.Equal(JsonValueKind.Object, expected.ValueKind);
        AssertWire(expected, Assert.IsType<Dictionary<string, object?>>(actual), ledger);
    }

    /// <summary>
    /// One wire document against the recorded one: the same keys present (the
    /// omission rules are behaviour), the same values, and instants checked as
    /// relations rather than literals — see <see cref="Ledger"/>.
    /// </summary>
    private static void AssertWire(JsonElement expected, Dictionary<string, object?> actual, Ledger ledger)
    {
        Assert.Equal(
            expected.EnumerateObject().Select(p => p.Name).OrderBy(n => n, StringComparer.Ordinal).ToList(),
            actual.Keys.OrderBy(n => n, StringComparer.Ordinal).ToList());
        foreach (var property in expected.EnumerateObject())
        {
            if (property.Name is "created_at" or "updated_at")
            {
                ledger.Check(property.Name, property.Value.GetString()!, (DateTimeOffset)actual[property.Name]!);
                continue;
            }

            Assert.Equal(Canon(JsonToClr(property.Value)), Canon(actual[property.Name]));
        }
    }

    /// <summary>
    /// What the corpus can say about times that this port can answer.
    ///
    /// Outside the registry (<see cref="Literal"/>) every instant is the
    /// caller's own and is compared as the literal it is.
    ///
    /// Inside a registry scenario (<see cref="Stamped"/>) it is not: the
    /// reference registry takes an injectable clock and the corpus was
    /// recorded against one that ticks a second per reading, whereas the C#
    /// registry reads <c>DateTimeOffset.UtcNow</c> directly and has no seam to
    /// inject through. The recorded literals therefore cannot be reproduced —
    /// so what is asserted is everything those literals encode that is not the
    /// literal:
    ///
    /// * A recorded instant of WHEN itself is the caller's own
    ///   <c>created_at</c>, kept rather than stamped (seeding does this) — so
    ///   the port must return exactly WHEN.
    /// * Any later instant was stamped by the registry — so the port's must
    ///   fall inside this run, which WHEN (a date in the past) never does.
    ///   That is what "the registry stamps the time, not the caller" means,
    ///   and it is the assertion that a registry echoing the caller fails.
    /// * Two recorded instants that are equal must come back equal — this is
    ///   what makes "an update keeps the creation time" and "a read returns
    ///   the stored one" checkable.
    /// * A later recorded instant must not come back earlier than an earlier
    ///   one. Not strictly later: <c>UtcNow</c> has ~15ms granularity on
    ///   Windows, where two adjacent stamps legitimately land on one value.
    /// </summary>
    private sealed class Ledger
    {
        private readonly Dictionary<string, DateTimeOffset> _seen = new(StringComparer.Ordinal);
        private readonly DateTimeOffset _start = DateTimeOffset.UtcNow;
        private readonly bool _stamped;
        private DateTimeOffset _highWater = DateTimeOffset.MinValue;

        private Ledger(bool stamped) => _stamped = stamped;

        /// <summary>For sections where no registry runs, so nothing is stamped.</summary>
        public static Ledger Literal() => new(false);

        /// <summary>For a registry scenario, where later instants are stamped.</summary>
        public static Ledger Stamped() => new(true);

        public void Check(string field, string recorded, DateTimeOffset actual)
        {
            var wanted = DateTimeOffset.Parse(recorded, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind);
            if (!_stamped || wanted == When)
            {
                Assert.Equal(wanted, actual);
                return;
            }

            if (_seen.TryGetValue(recorded, out var first))
            {
                Assert.Equal(first, actual);
                return;
            }

            Assert.InRange(actual, _start, DateTimeOffset.UtcNow);
            Assert.True(actual >= _highWater, $"{field} {actual:O} went backwards from {_highWater:O}");
            _seen[recorded] = actual;
            _highWater = actual;
        }
    }

    // --- what a graphical target may say ------------------------------------

    [Fact]
    public void SpeaksTheProtocolsTheReferenceSpeaks()
    {
        Assert.Equal(
            Golden.GetProperty("protocols").EnumerateArray().Select(p => p.GetString()!).ToList(),
            GraphicalTargetConstants.SupportedProtocols.OrderBy(p => p, StringComparer.Ordinal).ToList());
    }

    [Fact]
    public void RefusesWithTheCodesTheReferenceRefusesWith()
    {
        Assert.Equal(
            Golden.GetProperty("error_codes").EnumerateArray().Select(c => c.GetString()!).ToList(),
            Enum.GetNames<GraphicalTargetErrorCode>().Select(Screaming).ToList());
    }

    public static TheoryData<int> DefinitionIndexes => Indexes("definitions");

    [Theory]
    [MemberData(nameof(DefinitionIndexes))]
    public void Definition(int index)
    {
        var record = Golden.GetProperty("definitions")[index];
        var target = TargetFrom(record.GetProperty("fields"));
        string? error = null;
        string? message = null;
        try
        {
            target.Validate();
        }
        catch (GraphicalTargetException ex)
        {
            error = Screaming(ex.Code.ToString());
            message = ex.Message;
        }
        catch (ArgumentException ex)
        {
            // Validate() reports the non-endpoint rules as ArgumentException and
            // the registry rewraps them as Invalid (see CreateCore/UpdateCore),
            // which is the code every caller of this port actually observes.
            error = Screaming(GraphicalTargetErrorCode.Invalid.ToString());
            message = ex.Message;
        }

        if (record.TryGetProperty("error", out var expectedError))
        {
            Assert.Equal(expectedError.GetString(), error);
            Assert.Equal(record.GetProperty("message").GetString(), message);
            return;
        }

        Assert.Null(error);
        AssertWire(record.GetProperty("wire"), ToWire(target), Ledger.Literal());
        // Validation normalises in place, as the reference does.
        Assert.Equal(record.GetProperty("protocol").GetString(), target.Protocol);
        Assert.Equal(record.GetProperty("endpoint").GetString(), target.Endpoint);
    }

    [Fact]
    public void StripsEverySecretAndKeepsTheSettings()
    {
        var target = new Def
        {
            TargetId = "vm1",
            Endpoint = "vm.example:5900",
            Secret = "s3cret", // pragma: allowlist secret
            CaSecretRef = "env:CA", // pragma: allowlist secret
            ClientCertSecretRef = "env:CERT", // pragma: allowlist secret
            ClientKeySecretRef = "env:KEY", // pragma: allowlist secret
            CreatedAt = When,
            Config = new Dictionary<string, object?>(StringComparer.Ordinal) { ["vm_name"] = "guest-1" },
        };

        AssertWire(Golden.GetProperty("public_copy").GetProperty("value"), ToWire(target.PublicCopy()), Ledger.Literal());
    }

    // --- where an endpoint points -------------------------------------------
    //
    // NOT executed here, and deliberately not quarantined either: the corpus's
    // `rfb` (71 cases) and `litevirt` (22 cases) sections were driven through
    // GraphicalTargetParsing during this wiring and 22 + 5 of them disagree
    // with the reference, in three classes:
    //
    //   * the host of a bracketed IPv6 endpoint — System.Uri.Host keeps the
    //     brackets ("[2001:db8::1]") where the reference strips them, and that
    //     string is what a connection is opened to;
    //   * which of the two refusals a malformed port earns — the reference
    //     says "invalid endpoint port", this port says "invalid endpoint;
    //     expected host:port…" because Uri.TryCreate rejects before any port
    //     is looked at;
    //   * a handful of accept/refuse disagreements around IPvFuture, zone ids
    //     and bracketed credentials.
    //
    // Every one of those needs a change under src/, which this pass is not
    // permitted to make, and none of them is the registry behaviour this
    // roadmap item is about. Wiring them is its own item — see the report that
    // accompanied CSHARP-GOLDEN-001. Adding them here as skipped theories
    // would only make the gap look handled.

    // --- who may see a target ------------------------------------------------

    public static TheoryData<int> ScopeIndexes => Indexes("scopes");

    [Theory]
    [MemberData(nameof(ScopeIndexes))]
    public void Scope(int index)
    {
        var record = Golden.GetProperty("scopes")[index];
        var scope = MakeScope(
            record.GetProperty("scope_tenant").GetString(), record.GetProperty("is_system").GetBoolean());
        Assert.Equal(record.GetProperty("is_valid").GetBoolean(), scope.IsValid);
        Assert.Equal(
            record.GetProperty("permits").GetBoolean(),
            scope.Permits(record.GetProperty("target_tenant").GetString()));
    }

    /// <summary>
    /// A scope with the corpus's exact pair of values.
    ///
    /// The public surface cannot express "a scope that is both" — there is no
    /// factory for it, which is a stronger position than the reference's and
    /// deliberately kept. The private constructor is reached here so the
    /// recorded row is still executed rather than assumed: what is being
    /// checked is that <c>IsValid</c>/<c>Permits</c> refuse it, not merely
    /// that nobody can type it.
    /// </summary>
    private static GraphicalTargetScope MakeScope(string? tenantId, bool isSystem)
    {
        if (tenantId is null && isSystem)
        {
            return GraphicalTargetScope.System();
        }

        if (tenantId is not null && !isSystem)
        {
            Assert.True(GraphicalTargetScope.TryForTenant(tenantId, out var tenantScope));
            return tenantScope;
        }

        if (tenantId is null)
        {
            // Neither: what an unauthenticated caller arrives with.
            return default;
        }

        var ctor = typeof(GraphicalTargetScope).GetConstructor(
            BindingFlags.NonPublic | BindingFlags.Instance, null, [typeof(string), typeof(bool)], null);
        return (GraphicalTargetScope)ctor!.Invoke([tenantId, isSystem]);
    }

    public static TheoryData<int> ScopeForTenantIndexes => Indexes("scope_for_tenant");

    [Theory]
    [MemberData(nameof(ScopeForTenantIndexes))]
    public void ScopeForTenant(int index)
    {
        var record = Golden.GetProperty("scope_for_tenant")[index];
        var ok = GraphicalTargetScope.TryForTenant(record.GetProperty("tenant").GetString()!, out var scope);
        Assert.Equal(record.GetProperty("ok").GetBoolean(), ok);
        if (!ok)
        {
            return;
        }

        var expected = record.GetProperty("scope");
        Assert.Equal(expected.GetProperty("tenant_id").GetString(), scope.TenantId);
        Assert.Equal(expected.GetProperty("is_system").GetBoolean(), scope.IsSystem);
    }

    [Fact]
    public void NamesTheSystemScopeAsTheReferenceNamesIt()
    {
        var expected = Golden.GetProperty("system_scope");
        Assert.Equal(JsonValueKind.Null, expected.GetProperty("tenant_id").ValueKind);
        Assert.Null(GraphicalTargetScope.System().TenantId);
        Assert.Equal(expected.GetProperty("is_system").GetBoolean(), GraphicalTargetScope.System().IsSystem);
    }

    // --- the registry ---------------------------------------------------------

    private static GraphicalTargetScope ScenarioScope(string name)
    {
        switch (name)
        {
            case "system":
                return GraphicalTargetScope.System();
            case "broken":
                // Neither the system nor a tenant.
                return default;
            default:
                Assert.True(GraphicalTargetScope.TryForTenant(name, out var scope));
                return scope;
        }
    }

    public static TheoryData<int> ScenarioIndexes => Indexes("scenarios");

    [Theory]
    [MemberData(nameof(ScenarioIndexes))]
    public async Task Scenario(int index)
    {
        var scenario = Golden.GetProperty("scenarios")[index];
        var registry = new InMemoryGraphicalTargetRegistry();
        var ledger = Ledger.Stamped();
        foreach (var step in scenario.GetProperty("steps").EnumerateArray())
        {
            var scope = step.TryGetProperty("scope", out var scopeName)
                ? ScenarioScope(scopeName.GetString()!)
                : GraphicalTargetScope.System();
            var target = step.TryGetProperty("fields", out var fields) ? TargetFrom(fields) : null;
            var targetId = step.TryGetProperty("target_id", out var id) ? id.GetString()! : "";
            Func<Task<object?>> operation = step.GetProperty("op").GetString() switch
            {
                "create" => async () => ToWire(await registry.CreateAsync(scope, target!)),
                "update" => async () => ToWire(await registry.UpdateAsync(scope, target!)),
                "delete" => async () => { await registry.DeleteAsync(scope, targetId); return null; },
                "add_static" => async () => { await registry.AddStaticAsync(target!); return null; },
                "close" => () => { registry.Close(); return Task.FromResult<object?>(null); },
                "get" => async () =>
                {
                    var found = await registry.GetAsync(scope, targetId);
                    return found is null ? null : ToWire(found);
                },
                "list" => async () =>
                    (IReadOnlyList<Dictionary<string, object?>>)(await registry.ListAsync(scope))
                        .Select(ToWire).ToList(),
                var op => throw new InvalidOperationException($"unknown corpus op {op}"),
            };
            await ExpectOutcomeAsync(step, ledger, operation);
        }
    }

    /// <summary>
    /// Every index of a corpus section. The theory data is the index rather
    /// than the record so xUnit gets something it can serialise, and so a
    /// corpus that grew is a set of new cases rather than a silent no-op.
    /// </summary>
    private static TheoryData<int> Indexes(string section)
    {
        var data = new TheoryData<int>();
        for (var i = 0; i < Golden.GetProperty(section).GetArrayLength(); i++)
        {
            data.Add(i);
        }

        return data;
    }
}
