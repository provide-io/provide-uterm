//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.ServerAuth;

/// <summary>Unordered string set used for roles/scopes/capabilities.</summary>
public sealed class StringSet : HashSet<string>
{
    public StringSet() : base(StringComparer.Ordinal) { }

    public StringSet(IEnumerable<string> items) : base(items, StringComparer.Ordinal) { }

    public static StringSet Of(params string[] items) => new(items);

    public bool Has(string item) => Contains(item);

    public IReadOnlyList<string> Sorted()
    {
        var list = this.ToList();
        list.Sort(StringComparer.Ordinal);
        return list;
    }
}

/// <summary>Resolved browser or API principal.</summary>
public sealed class Principal
{
    public required string SubjectId { get; set; }
    public string? TenantId { get; set; }
    public StringSet Roles { get; set; } = new();
    public StringSet Scopes { get; set; } = new();
    public Dictionary<string, object?> Claims { get; set; } = new();
    public string? DisplayName { get; set; }
    public string? AdminSessionScope { get; set; }

    public string Name => string.IsNullOrEmpty(DisplayName) ? SubjectId : DisplayName;

    public static Principal Anonymous() => new()
    {
        SubjectId = "anonymous",
        Roles = StringSet.Of("viewer"),
        Scopes = new StringSet(),
    };
}

/// <summary>Transport-agnostic request view for authenticators.</summary>
public sealed class AuthRequest
{
    public Dictionary<string, string> Headers { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public Dictionary<string, string> Cookies { get; set; } = new(StringComparer.Ordinal);
    public string SourceIp { get; set; } = "";

    public string Header(string key) =>
        Headers.TryGetValue(key, out var v) ? v : "";

    public string Cookie(string key) =>
        Cookies.TryGetValue(key, out var v) ? v.Trim() : "";
}

public interface IAuthenticator
{
    Task<Principal> AuthenticateAsync(AuthRequest request, CancellationToken cancellationToken = default);
}
