// SPDX-File-Identifier: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

using System.Collections.Generic;
using System.Globalization;
using System.Net;
using System.Text.RegularExpressions;

namespace Provide.Uterm.Server;

public static class GraphicalTargetModels
{
    public static readonly Regex GraphicalNamePattern = new(@"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", RegexOptions.Compiled);
    public static readonly Regex VmPatternPattern = new(@"^[A-Za-z0-9_*?.:-]{1,256}$", RegexOptions.Compiled);
    public static readonly Regex LabelPattern = new(@"^[A-Za-z_][A-Za-z0-9_]*$", RegexOptions.Compiled);
    public static readonly Regex DnsLabelPattern = new(@"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$", RegexOptions.Compiled);

    public static readonly ISet<string> AllowedTlsModes =
    [
        "tls",
        "mtls",
        "disabled",
    ].ToHashSet(StringComparer.Ordinal);

    public static readonly HashSet<string> GraphicalTargetPayloadKeys = new(
    [
        "tenant_id",
        "target_id",
        "endpoint",
        "tls_mode",
        "ca_secret_ref",
        "client_cert_secret_ref",
        "client_key_secret_ref",
        "expected_server_name",
        "allowed_vm_patterns",
        "minimum_role",
        "connect_timeout_s",
        "handshake_timeout_s",
        "read_timeout_s",
        "write_timeout_s",
        "shutdown_timeout_s",
        "max_grpc_message_bytes",
        "max_framebuffer_width",
        "max_framebuffer_height",
        "max_rectangles",
        "max_clipboard_bytes",
        "max_pixel_allocation_bytes",
        "allowed_cidrs",
        "audit_labels",
    ],
    StringComparer.Ordinal);
}

public sealed class GraphicalTargetDefinition
{
    public string TargetId { get; set; } = "";
    public string Endpoint { get; set; } = "";
    public string TlsMode { get; set; } = "tls";
    public string? CaSecretRef { get; set; }
    public string? ClientCertSecretRef { get; set; }
    public string? ClientKeySecretRef { get; set; }
    public string? ExpectedServerName { get; set; }
    public List<string> AllowedVMPatterns { get; set; } = new() { "*" };
    public string? TenantId { get; set; }
    public string MinimumRole { get; set; } = "viewer";
    public double ConnectTimeoutS { get; set; } = 10;
    public double HandshakeTimeoutS { get; set; } = 10;
    public double ReadTimeoutS { get; set; } = 30;
    public double WriteTimeoutS { get; set; } = 30;
    public double ShutdownTimeoutS { get; set; } = 5;
    public long MaxGRPCMessageBytes { get; set; } = 16 << 20;
    public long MaxFramebufferWidth { get; set; } = 8192;
    public long MaxFramebufferHeight { get; set; } = 8192;
    public long MaxRectangles { get; set; } = 4096;
    public long MaxClipboardBytes { get; set; } = 1 << 20;
    public long MaxPixelAllocationBytes { get; set; } = 256 << 20;
    public List<string> AllowedCIDRs { get; set; } = new();
    public Dictionary<string, string> AuditLabels { get; set; } = new();

    public GraphicalTargetDefinition Clone() => new()
    {
        TargetId = TargetId,
        Endpoint = Endpoint,
        TlsMode = TlsMode,
        CaSecretRef = CaSecretRef,
        ClientCertSecretRef = ClientCertSecretRef,
        ClientKeySecretRef = ClientKeySecretRef,
        ExpectedServerName = ExpectedServerName,
        AllowedVMPatterns = AllowedVMPatterns.ToList(),
        TenantId = TenantId,
        MinimumRole = MinimumRole,
        ConnectTimeoutS = ConnectTimeoutS,
        HandshakeTimeoutS = HandshakeTimeoutS,
        ReadTimeoutS = ReadTimeoutS,
        WriteTimeoutS = WriteTimeoutS,
        ShutdownTimeoutS = ShutdownTimeoutS,
        MaxGRPCMessageBytes = MaxGRPCMessageBytes,
        MaxFramebufferWidth = MaxFramebufferWidth,
        MaxFramebufferHeight = MaxFramebufferHeight,
        MaxRectangles = MaxRectangles,
        MaxClipboardBytes = MaxClipboardBytes,
        MaxPixelAllocationBytes = MaxPixelAllocationBytes,
        AllowedCIDRs = AllowedCIDRs.ToList(),
        AuditLabels = AuditLabels.ToDictionary(p => p.Key, p => p.Value),
    };

    public GraphicalTargetDefinition Public()
    {
        var copy = Clone();
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

        if (string.IsNullOrWhiteSpace(Endpoint))
        {
            throw new ArgumentException("endpoint must use dns:///host:port syntax");
        }

        var endpoint = Endpoint.Trim();
        if (!endpoint.StartsWith("dns:///", StringComparison.Ordinal))
        {
            throw new ArgumentException("endpoint must use dns:///host:port syntax");
        }

        var noScheme = endpoint["dns:///".Length..];
        if (string.IsNullOrWhiteSpace(noScheme) || noScheme.Contains('?') || noScheme.Contains('#') || noScheme.Contains('@')
            || noScheme.Contains('/') )
        {
            throw new ArgumentException("endpoint must use dns:///host:port syntax");
        }

        string host;
        int port;
        if (noScheme.StartsWith("[", StringComparison.Ordinal))
        {
            var end = noScheme.IndexOf(']');
            if (end < 0 || end + 1 >= noScheme.Length || noScheme[end + 1] != ':')
            {
                throw new ArgumentException("endpoint must include a valid host and port");
            }

            host = noScheme[1..end];
            var portText = noScheme[(end + 2)..];
            if (!int.TryParse(portText, NumberStyles.None, CultureInfo.InvariantCulture, out port) || port is < 1 or > 65535)
            {
                throw new ArgumentException("endpoint must include a valid host and port");
            }

            if (!IPAddress.TryParse(host, out var address) || address.AddressFamily != System.Net.Sockets.AddressFamily.InterNetworkV6)
            {
                throw new ArgumentException("endpoint must include a valid IPv6 host when bracketed");
            }
        }
        else
        {
            var idx = noScheme.LastIndexOf(':');
            if (idx <= 0 || idx == noScheme.Length - 1)
            {
                throw new ArgumentException("endpoint must include a valid host and port");
            }

            host = noScheme[..idx];
            if (host.Contains(':'))
            {
                throw new ArgumentException("endpoint must include a valid host and port");
            }

            var portText = noScheme[(idx + 1)..];
            if (!int.TryParse(portText, NumberStyles.None, CultureInfo.InvariantCulture, out port)
                || port is < 1 or > 65535)
            {
                throw new ArgumentException("endpoint must include a valid host and port");
            }

            if (IPAddress.TryParse(host, out _))
            {
                if (!IPAddress.TryParse(host, out _))
                {
                    throw new ArgumentException("endpoint must include a valid host and port");
                }
            }
            else
            {
                var parts = host.Split('.', StringSplitOptions.RemoveEmptyEntries);
                if (parts.Length == 0 || host.Length > 253)
                {
                    throw new ArgumentException("endpoint must use dns:///host:port syntax");
                }

                foreach (var part in parts)
                {
                    if (!GraphicalTargetModels.DnsLabelPattern.IsMatch(part))
                    {
                        throw new ArgumentException("endpoint must include a valid host and port");
                    }
                }
            }
        }

        if (port == 0)
        {
            throw new ArgumentException("endpoint must include a valid host and port");
        }

        if (!GraphicalTargetModels.AllowedTlsModes.Contains(TlsMode))
        {
            throw new ArgumentException("tls_mode must be disabled|tls|mtls");
        }

        if (TenantId is not null && !GraphicalTargetModels.GraphicalNamePattern.IsMatch(TenantId))
        {
            throw new ArgumentException("tenant_id must be a safe identifier");
        }

        if (MinimumRole != "viewer" && MinimumRole != "operator" && MinimumRole != "admin")
        {
            throw new ArgumentException("minimum_role must be viewer, operator, or admin");
        }

        if (!IsValidSecretRef(CaSecretRef) || !IsValidSecretRef(ClientCertSecretRef) || !IsValidSecretRef(ClientKeySecretRef))
        {
            throw new ArgumentException("invalid secret reference syntax");
        }

        if (TlsMode == "disabled" && (CaSecretRef is not null || ExpectedServerName is not null))
        {
            throw new ArgumentException("disabled TLS may not specify CA or server name");
        }

        var hasClientRef = ClientCertSecretRef is not null || ClientKeySecretRef is not null;
        if (TlsMode != "mtls" && hasClientRef)
        {
            throw new ArgumentException("client certificate references require mtls");
        }

        if (TlsMode == "mtls" && (ClientCertSecretRef is null || ClientKeySecretRef is null))
        {
            throw new ArgumentException("mtls requires both client certificate and key references");
        }

        if (ExpectedServerName is { } expected && !IsValidIdentity(expected))
        {
            throw new ArgumentException("expected_server_name is invalid");
        }

        if (AllowedVMPatterns.Count == 0)
        {
            throw new ArgumentException("allowed_vm_patterns must contain safe glob patterns");
        }

        var dedupVms = new HashSet<string>(StringComparer.Ordinal);
        foreach (var pattern in AllowedVMPatterns)
        {
            if (!GraphicalTargetModels.VmPatternPattern.IsMatch(pattern))
            {
                throw new ArgumentException("allowed_vm_patterns must contain safe glob patterns");
            }

            dedupVms.Add(pattern);
        }

        AllowedVMPatterns = dedupVms.ToList();

        foreach (var timeout in new[] { ConnectTimeoutS, HandshakeTimeoutS, ReadTimeoutS, WriteTimeoutS, ShutdownTimeoutS })
        {
            if (timeout <= 0)
            {
                throw new ArgumentException("values must be positive");
            }
        }

        foreach (var limit in new[]
                 {
                     MaxGRPCMessageBytes,
                     MaxFramebufferWidth,
                     MaxFramebufferHeight,
                     MaxRectangles,
                     MaxClipboardBytes,
                     MaxPixelAllocationBytes,
                 })
        {
            if (limit <= 0)
            {
                throw new ArgumentException("values must be positive");
            }
        }

        var normalizedCidrs = new List<string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var cidr in AllowedCIDRs)
        {
            if (!TryNormalizeCidr(cidr, out var canonical))
            {
                throw new ArgumentException("allowed_cidrs must contain canonical networks");
            }

            if (seen.Add(canonical))
            {
                normalizedCidrs.Add(canonical);
            }
        }

        AllowedCIDRs = normalizedCidrs;

        foreach (var (key, value) in AuditLabels)
        {
            if (key.Length > 0 && !GraphicalTargetModels.LabelPattern.IsMatch(key))
            {
                throw new ArgumentException("audit_labels contain an invalid label");
            }

            if (value.Length > 256)
            {
                throw new ArgumentException("audit_labels contain an invalid label");
            }
        }
    }

    private static bool IsValidIdentity(string value)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Length > 253)
        {
            return false;
        }

        if (!value.All(c => c < 128))
        {
            return false;
        }

        if (IPAddress.TryParse(value, out _))
        {
            return true;
        }

        return value.Split('.').All(GraphicalTargetModels.DnsLabelPattern.IsMatch);
    }

    private static bool IsValidSecretRef(string? value)
    {
        if (value is null)
        {
            return true;
        }

        if (value.StartsWith("env:", StringComparison.Ordinal))
        {
            return GraphicalTargetModels.LabelPattern.IsMatch(value[4..]);
        }

        if (value.StartsWith("file:", StringComparison.Ordinal))
        {
            var path = value[5..];
            if (string.IsNullOrWhiteSpace(path))
            {
                return false;
            }

            return !path.Contains("\0") && (path.StartsWith('/') || !path.Contains("/../"));
        }

        return false;
    }

    private static bool TryNormalizeCidr(string? raw, out string normalized)
    {
        normalized = "";
        if (string.IsNullOrWhiteSpace(raw)) return false;
        var text = raw.Trim();
        var idx = text.IndexOf('/');
        if (idx < 1 || idx == text.Length - 1)
        {
            return false;
        }

        var host = text[..idx];
        if (!int.TryParse(text[(idx + 1)..], NumberStyles.None, CultureInfo.InvariantCulture, out var bits))
        {
            return false;
        }

        if (IPAddress.TryParse(host, out var ip))
        {
            int maxBits = ip.AddressFamily == System.Net.Sockets.AddressFamily.InterNetworkV6 ? 128 : 32;
            if (bits < 0 || bits > maxBits)
            {
                return false;
            }

            var bytes = ip.GetAddressBytes();
            Span<byte> masked = bits switch
            {
                0 => (Span<byte>)new byte[bytes.Length],
                128 when bytes.Length == 16 => bytes,
                _ => bytes,
            };
            for (var i = bits; i < maxBits; i++)
            {
                var byteIdx = i / 8;
                var bitIdx = 7 - (i % 8);
                masked[byteIdx] &= (byte)~(1 << bitIdx);
            }

            normalized = new IPAddress(masked.ToArray()).ToString() + "/" + bits;
            if (string.Equals(text, normalized, StringComparison.Ordinal))
            {
                return true;
            }

            return false;
        }

        return false;
    }
}

public enum GraphicalTargetErrorCode
{
    AlreadyExists,
    NotFound,
    Immutable,
    Forbidden,
    Transaction,
    Invalid,
    Closed,
    Backend,
    Persisted,
}

public sealed class GraphicalTargetException : Exception
{
    public GraphicalTargetErrorCode Code { get; }

    public GraphicalTargetException(GraphicalTargetErrorCode code, string message) : base(message)
    {
        Code = code;
    }
}

public readonly struct GraphicalTargetScope
{
    public string? TenantId { get; }
    public bool IsSystem { get; }

    private GraphicalTargetScope(string tenantId, bool isSystem)
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

    public static GraphicalTargetScope System() => new(string.Empty, true);

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
            clone.Validate();
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
    }

    public GraphicalTargetDefinition? Get(GraphicalTargetScope scope, string targetId)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            if (!scope.IsValid)
            {
                return null;
            }

            if (_static.TryGetValue(targetId, out var staticTarget))
            {
                return scope.Permits(staticTarget.TenantId) ? staticTarget.Clone() : null;
            }

            if (_runtime.TryGetValue(targetId, out var runtimeTarget))
            {
                return scope.Permits(runtimeTarget.TenantId) ? runtimeTarget.Clone() : null;
            }

            return null;
        }
    }

    public IReadOnlyList<GraphicalTargetDefinition> List(GraphicalTargetScope scope)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            if (!scope.IsValid)
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            var merged = new Dictionary<string, GraphicalTargetDefinition>(StringComparer.Ordinal);
            foreach (var pair in _runtime)
            {
                if (!scope.Permits(pair.Value.TenantId)) continue;
                merged[pair.Key] = pair.Value.Clone();
            }

            foreach (var pair in _static)
            {
                if (!scope.Permits(pair.Value.TenantId)) continue;
                merged[pair.Key] = pair.Value.Clone();
            }

            var result = merged.Values.OrderBy(x => x.TargetId).ToList();
            return result;
        }
    }

    public GraphicalTargetDefinition Create(GraphicalTargetScope scope, GraphicalTargetDefinition target)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            var clone = target.Clone();
            if (!scope.IsValid)
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

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
            if (!scope.IsValid)
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

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

            _runtime[clone.TargetId] = clone;
            return clone.Clone();
        }
    }

    public void Delete(GraphicalTargetScope scope, string targetId)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            if (!scope.IsValid)
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

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
}
