// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

using System.Globalization;
using System.Net;
using System.Text.RegularExpressions;

namespace Provide.Uterm.Server;

public static class GraphicalTargetConstants
{
    public const string ProtocolMemory = "memory";
    public const string ProtocolRfb = "rfb";
    public const string ProtocolLitevirt = "litevirt";

    public static readonly HashSet<string> SupportedProtocols = new(StringComparer.OrdinalIgnoreCase)
    {
        ProtocolMemory,
        ProtocolRfb,
        ProtocolLitevirt,
    };

    public const string ErrorInvalidPayload = "graphical_target_invalid";
    public const string ErrorAlreadyExists = "graphical_target_exists";
    public const string ErrorNotFound = "graphical_target_not_found";
    public const string ErrorImmutable = "graphical_target_immutable";
    public const string ErrorConflict = "graphical_target_conflict";
    public const string ErrorUnavailable = "graphical_target_unavailable";
    public const string ErrorBackend = "graphical_target_backend_error";
    public const string ErrorTenantManaged = "tenant_managed";
    public const string ErrorTargetIdMismatch = "target_id_mismatch";
}

public static class GraphicalTargetModels
{
    public static readonly Regex GraphicalNamePattern = new(@"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", RegexOptions.Compiled);
    public static readonly Regex TenantNamePattern = GraphicalNamePattern;
    public static readonly Regex SecretRefPattern = new(@"^(?:env:[A-Za-z_][A-Za-z0-9_]*|file:/[^\x00]+)$", RegexOptions.Compiled);

    public static readonly HashSet<string> GraphicalTargetPayloadKeys = new(StringComparer.Ordinal)
    {
        "tenant_id",
        "target_id",
        "display_name",
        "protocol",
        "endpoint",
        "secret",
        "width",
        "height",
        "ca_secret_ref",
        "client_cert_secret_ref",
        "client_key_secret_ref",
        "is_system",
        "is_static",
        "config",
    };
}

public sealed class GraphicalTargetDefinition
{
    public string TargetId { get; set; } = "";
    public string TenantId { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string Protocol { get; set; } = GraphicalTargetConstants.ProtocolRfb;
    public string? Endpoint { get; set; }
    public string? Secret { get; set; }
    public int Width { get; set; } = 640;
    public int Height { get; set; } = 480;
    public bool IsSystem { get; set; }
    public bool IsStatic { get; set; }

    public string? CaSecretRef { get; set; }
    public string? ClientCertSecretRef { get; set; }
    public string? ClientKeySecretRef { get; set; }

    // Generic per-target, protocol-specific parameters (JSON key "config").
    // Carries e.g. litevirt vm_name. NOT a secret — kept in Clone AND PublicCopy.
    public Dictionary<string, object?> Config { get; set; } = new();

    public string? CreatedBy { get; set; }
    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;
    public string? UpdatedBy { get; set; }
    public DateTimeOffset? UpdatedAt { get; set; }

    public GraphicalTargetDefinition Clone() => new()
    {
        TargetId = TargetId,
        TenantId = TenantId,
        DisplayName = DisplayName,
        Protocol = Protocol,
        Endpoint = Endpoint,
        Secret = Secret,
        Width = Width,
        Height = Height,
        IsSystem = IsSystem,
        IsStatic = IsStatic,
        CaSecretRef = CaSecretRef,
        ClientCertSecretRef = ClientCertSecretRef,
        ClientKeySecretRef = ClientKeySecretRef,
        Config = new Dictionary<string, object?>(Config),
        CreatedBy = CreatedBy,
        CreatedAt = CreatedAt,
        UpdatedBy = UpdatedBy,
        UpdatedAt = UpdatedAt,
    };

    public GraphicalTargetDefinition PublicCopy()
    {
        var copy = Clone();
        copy.Secret = null;
        copy.CaSecretRef = null;
        copy.ClientCertSecretRef = null;
        copy.ClientKeySecretRef = null;
        return copy;
    }

    public void Validate()
    {
        if (!GraphicalTargetModels.GraphicalNamePattern.IsMatch(TargetId))
        {
            throw new ArgumentException("target_id must be a safe identifier");
        }

        var protocol = Protocol.Trim().ToLowerInvariant();
        if (!GraphicalTargetConstants.SupportedProtocols.Contains(protocol))
        {
            throw new ArgumentException("unsupported protocol");
        }
        Protocol = protocol;

        if (protocol == GraphicalTargetConstants.ProtocolRfb)
        {
            var parsed = GraphicalTargetParsing.ParseRfbEndpoint(Endpoint);
            Endpoint = $"{parsed.Host}:{parsed.Port}";
        }
        else if (protocol == GraphicalTargetConstants.ProtocolLitevirt)
        {
            // A litevirt endpoint is a plain host:port gRPC target (no rfb:// scheme).
            // Require it non-empty and shaped like host:port, but do not impose a scheme.
            var parsed = GraphicalTargetParsing.ParseLitevirtEndpoint(Endpoint);
            Endpoint = $"{parsed.Host}:{parsed.Port}";
        }

        if (Width < 1 || Width > 8192)
        {
            throw new ArgumentException("width out of range");
        }

        if (Height < 1 || Height > 8192)
        {
            throw new ArgumentException("height out of range");
        }

        if (!string.IsNullOrWhiteSpace(TenantId) && !GraphicalTargetModels.TenantNamePattern.IsMatch(TenantId))
        {
            throw new ArgumentException("tenant_id is invalid");
        }

        foreach (var secret in new[] { CaSecretRef, ClientCertSecretRef, ClientKeySecretRef })
        {
            if (secret is not null && !GraphicalTargetModels.SecretRefPattern.IsMatch(secret))
            {
                throw new ArgumentException("invalid secret reference syntax");
            }
        }
    }
}

public enum GraphicalTargetErrorCode
{
    AlreadyExists,
    NotFound,
    Immutable,
    Forbidden,
    Conflict,
    Invalid,
    Closed,
    Backend,
}

public sealed class GraphicalTargetException : Exception
{
    public GraphicalTargetErrorCode Code { get; }
    public GraphicalTargetException(GraphicalTargetErrorCode code, string message) : base(message) => Code = code;
}

public readonly struct GraphicalTargetScope
{
    public string? TenantId { get; }
    public bool IsSystem { get; }

    private GraphicalTargetScope(string? tenantId, bool isSystem)
    {
        TenantId = tenantId;
        IsSystem = isSystem;
    }

    public static bool TryForTenant(string tenantId, out GraphicalTargetScope scope)
    {
        scope = default;
        if (string.IsNullOrWhiteSpace(tenantId))
        {
            return false;
        }

        scope = new GraphicalTargetScope(tenantId, false);
        return true;
    }

    public static GraphicalTargetScope System() => new(null, true);

    public bool IsValid => IsSystem != (TenantId is not null);
    public bool Permits(string? tenantId) => IsValid && (IsSystem || tenantId is not null && tenantId == TenantId);
}

public interface IGraphicalTargetRegistry
{
    GraphicalTargetDefinition? Get(GraphicalTargetScope scope, string targetId);
    IReadOnlyList<GraphicalTargetDefinition> List(GraphicalTargetScope scope);
    GraphicalTargetDefinition Create(GraphicalTargetScope scope, GraphicalTargetDefinition target);
    GraphicalTargetDefinition Update(GraphicalTargetScope scope, GraphicalTargetDefinition target);
    void Delete(GraphicalTargetScope scope, string targetId);
    void AddStatic(GraphicalTargetDefinition target);
}

public sealed class InMemoryGraphicalTargetRegistry : IGraphicalTargetRegistry
{
    private readonly object _gate = new();
    private readonly Dictionary<string, GraphicalTargetDefinition> _static = new(StringComparer.Ordinal);
    private readonly Dictionary<string, GraphicalTargetDefinition> _runtime = new(StringComparer.Ordinal);
    private bool _closed;

    public InMemoryGraphicalTargetRegistry(IEnumerable<GraphicalTargetDefinition>? staticTargets = null)
    {
        if (staticTargets is null)
        {
            return;
        }

        foreach (var target in staticTargets)
        {
            var clone = target.Clone();
            clone.IsSystem = true;
            clone.Validate();
            if (_static.ContainsKey(clone.TargetId))
            {
                throw new InvalidOperationException("duplicate graphical target_id");
            }

            _static[clone.TargetId] = clone;
        }
    }

    public GraphicalTargetDefinition? Get(GraphicalTargetScope scope, string targetId)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            if (_static.TryGetValue(targetId, out var staticTarget) && scope.Permits(staticTarget.TenantId))
            {
                return staticTarget.Clone();
            }

            if (_runtime.TryGetValue(targetId, out var runtimeTarget) && scope.Permits(runtimeTarget.TenantId))
            {
                return runtimeTarget.Clone();
            }

            return null;
        }
    }

    public IReadOnlyList<GraphicalTargetDefinition> List(GraphicalTargetScope scope)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            var merged = new Dictionary<string, GraphicalTargetDefinition>(StringComparer.Ordinal);
            foreach (var pair in _runtime)
            {
                if (!scope.Permits(pair.Value.TenantId))
                {
                    continue;
                }

                merged[pair.Key] = pair.Value.Clone();
            }

            foreach (var pair in _static)
            {
                if (!scope.Permits(pair.Value.TenantId))
                {
                    continue;
                }

                merged[pair.Key] = pair.Value.Clone();
            }

            var outList = merged.Values.OrderBy(t => t.TargetId).Select(t => t.Clone()).ToList();
            return outList;
        }
    }

    public GraphicalTargetDefinition Create(GraphicalTargetScope scope, GraphicalTargetDefinition target)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            var clone = target.Clone();
            if (!scope.Permits(clone.TenantId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            try
            {
                clone.Validate();
            }
            catch (ArgumentException ex)
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, ex.Message);
            }

            if (_static.ContainsKey(clone.TargetId) || _runtime.ContainsKey(clone.TargetId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.AlreadyExists, "graphical target already exists");
            }

            clone.CreatedAt = DateTimeOffset.UtcNow;
            _runtime[clone.TargetId] = clone;
            return clone.Clone();
        }
    }

    public GraphicalTargetDefinition Update(GraphicalTargetScope scope, GraphicalTargetDefinition target)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            var clone = target.Clone();
            if (!scope.Permits(clone.TenantId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            try
            {
                clone.Validate();
            }
            catch (ArgumentException ex)
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, ex.Message);
            }

            if (_static.ContainsKey(clone.TargetId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Immutable, "static graphical target is immutable");
            }

            if (!_runtime.TryGetValue(clone.TargetId, out var current))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.NotFound, "graphical target not found");
            }

            if (!scope.Permits(current.TenantId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            clone.CreatedAt = current.CreatedAt;
            clone.CreatedBy = current.CreatedBy;
            clone.UpdatedAt = DateTimeOffset.UtcNow;
            _runtime[clone.TargetId] = clone;
            return clone.Clone();
        }
    }

    public void Delete(GraphicalTargetScope scope, string targetId)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            if (_static.TryGetValue(targetId, out var staticTarget))
            {
                if (!scope.Permits(staticTarget.TenantId))
                {
                    throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
                }

                throw new GraphicalTargetException(GraphicalTargetErrorCode.Immutable, "static graphical target is immutable");
            }

            if (!_runtime.TryGetValue(targetId, out var current))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.NotFound, "graphical target not found");
            }

            if (!scope.Permits(current.TenantId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            _runtime.Remove(targetId);
        }
    }

    public void AddStatic(GraphicalTargetDefinition target)
    {
        lock (_gate)
        {
            var clone = target.Clone();
            clone.Validate();
            clone.IsSystem = true;
            if (_static.ContainsKey(clone.TargetId))
            {
                throw new InvalidOperationException("duplicate graphical target_id");
            }

            _static[clone.TargetId] = clone;
        }
    }

    private void EnsureOpen(GraphicalTargetScope scope)
    {
        if (_closed)
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Closed, "graphical target registry is closed");
        }

        if (!scope.IsValid)
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
        }
    }
}

public static class GraphicalTargetParsing
{
    public static (string Host, int Port) ParseRfbEndpoint(string? rawEndpoint)
    {
        if (string.IsNullOrWhiteSpace(rawEndpoint))
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "endpoint is required for protocol rfb");
        }

        var endpoint = rawEndpoint.Trim();
        if (endpoint.StartsWith("dns:///", StringComparison.OrdinalIgnoreCase))
        {
            endpoint = endpoint[7..];
        }

        if (!endpoint.StartsWith("rfb://", StringComparison.OrdinalIgnoreCase))
        {
            if (!endpoint.Contains(':'))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "invalid endpoint; expected host:port or rfb://host:port");
            }

            endpoint = "rfb://" + endpoint;
        }

        if (!Uri.TryCreate(endpoint, UriKind.Absolute, out var uri) || string.IsNullOrWhiteSpace(uri.Host))
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "invalid endpoint; expected host:port or rfb://host:port");
        }

        if (uri.Port < 1 || uri.Port > 65535)
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "invalid endpoint port");
        }

        return (uri.Host, uri.Port);
    }

    /// <summary>
    /// Validate a litevirt gRPC endpoint. Unlike rfb this carries no scheme —
    /// it is a plain host:port target (optionally prefixed with dns:///). We
    /// require it non-empty and shaped like host:port with a valid port.
    /// </summary>
    public static (string Host, int Port) ParseLitevirtEndpoint(string? rawEndpoint)
    {
        if (string.IsNullOrWhiteSpace(rawEndpoint))
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "endpoint is required for protocol litevirt");
        }

        var endpoint = rawEndpoint.Trim();
        if (endpoint.StartsWith("dns:///", StringComparison.OrdinalIgnoreCase))
        {
            endpoint = endpoint[7..];
        }

        // Reuse a scheme wrapper purely to lean on Uri's host:port parsing; the
        // scheme is discarded (litevirt endpoints have no wire scheme).
        if (!Uri.TryCreate("grpc://" + endpoint, UriKind.Absolute, out var uri) || string.IsNullOrWhiteSpace(uri.Host))
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "invalid endpoint; expected host:port");
        }

        if (uri.Port < 1 || uri.Port > 65535)
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "invalid endpoint port");
        }

        return (uri.Host, uri.Port);
    }
}
