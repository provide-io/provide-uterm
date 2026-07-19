// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

using System.Text.Json;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.ServerAuth;

namespace Provide.Uterm.Server;

public sealed partial class UtermServer
{
    private const int MaxGraphicalTargetPage = 200;

    private async Task<IResult> HandleListGraphicalTargets(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!TryResolveGraphicalScope(p, "graphical.target.read", out var scope, out var accessError))
        {
            return accessError!;
        }

        var limit = 100;
        var offset = 0;
        if (ctx.Request.Query.TryGetValue("limit", out var limitRaw) && !string.IsNullOrWhiteSpace(limitRaw.ToString()))
        {
            if (!int.TryParse(limitRaw.ToString(), out limit) || limit < 1 || limit > MaxGraphicalTargetPage)
            {
                return DetailError(422, "limit must be between 1 and 200");
            }
        }

        if (ctx.Request.Query.TryGetValue("offset", out var offsetRaw) && !string.IsNullOrWhiteSpace(offsetRaw.ToString()))
        {
            if (!int.TryParse(offsetRaw.ToString(), out offset) || offset < 0)
            {
                return DetailError(422, "offset must be non-negative");
            }
        }

        IReadOnlyList<GraphicalTargetDefinition> rows;
        try
        {
            rows = _deps.GraphicalTargets.List(scope);
        }
        catch (GraphicalTargetException ex)
        {
            return GraphicalRouteError(ex);
        }

        var total = rows.Count;
        var start = offset > total ? total : offset;
        var end = Math.Min(start + limit, total);
        var items = rows.Skip(start).Take(Math.Max(0, end - start)).Select(r => r.PublicCopy()).ToList();
        return Results.Json(new { items, limit, offset, total }, JsonOpts);
    }

    private async Task<IResult> HandleGetGraphicalTarget(HttpContext ctx, string targetId)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!TryResolveGraphicalScope(p, "graphical.target.read", out var scope, out var accessError))
        {
            return accessError!;
        }

        try
        {
            var target = _deps.GraphicalTargets.Get(scope, targetId);
            if (target is null)
            {
                return GraphicalError(404, GraphicalTargetConstants.ErrorNotFound, "graphical target not found");
            }

            return Results.Json(target.PublicCopy(), JsonOpts);
        }
        catch (GraphicalTargetException ex)
        {
            return GraphicalRouteError(ex);
        }
    }

    private async Task<IResult> HandleCreateGraphicalTarget(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!TryResolveGraphicalScope(p, "graphical.target.manage", out var scope, out var accessError))
        {
            return accessError!;
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        foreach (var key in body.Keys)
        {
            if (!GraphicalTargetModels.GraphicalTargetPayloadKeys.Contains(key))
            {
                return GraphicalError(422, GraphicalTargetConstants.ErrorInvalidPayload, "invalid request body");
            }
        }

        if (!TryParseGraphicalTargetBody(body, out var payload, out var hasTargetId, out var hasTenant, out var parseError))
        {
            return GraphicalError(422, GraphicalTargetConstants.ErrorInvalidPayload, parseError ?? "invalid request body");
        }

        if (hasTenant)
        {
            return GraphicalError(422, GraphicalTargetConstants.ErrorTenantManaged, "tenant_id is assigned from authenticated identity");
        }

        if (hasTargetId)
        {
            return GraphicalError(422, GraphicalTargetConstants.ErrorInvalidPayload, "target_id is server-assigned and cannot be supplied");
        }

        payload.TenantId = scope.TenantId ?? "";
        payload.TargetId = payload.TargetId == "" ? GenerateGraphicalTargetId() : payload.TargetId;
        payload.IsSystem = false;
        payload.CreatedBy = p.SubjectId;
        payload.CreatedAt = DateTimeOffset.UtcNow;
        if (string.IsNullOrWhiteSpace(payload.DisplayName))
        {
            payload.DisplayName = "graphical-target";
        }
        try
        {
            var created = _deps.GraphicalTargets.Create(scope, payload);
            return Results.Json(created.PublicCopy(), JsonOpts, statusCode: StatusCodes.Status201Created);
        }
        catch (GraphicalTargetException ex)
        {
            return GraphicalRouteError(ex);
        }
    }

    private async Task<IResult> HandleUpdateGraphicalTarget(HttpContext ctx, string targetId)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!TryResolveGraphicalScope(p, "graphical.target.manage", out var scope, out var accessError))
        {
            return accessError!;
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        foreach (var key in body.Keys)
        {
            if (!GraphicalTargetModels.GraphicalTargetPayloadKeys.Contains(key))
            {
                return GraphicalError(422, GraphicalTargetConstants.ErrorInvalidPayload, "invalid request body");
            }
        }

        if (!TryParseGraphicalTargetBody(body, out var payload, out var hasTargetId, out var hasTenant, out var parseError))
        {
            return GraphicalError(422, GraphicalTargetConstants.ErrorInvalidPayload, parseError ?? "invalid request body");
        }

        if (hasTenant)
        {
            return GraphicalError(422, GraphicalTargetConstants.ErrorTenantManaged, "tenant_id is assigned from authenticated identity");
        }

        if (!string.IsNullOrWhiteSpace(payload.TargetId) && !string.Equals(payload.TargetId, targetId, StringComparison.Ordinal))
        {
            return GraphicalError(409, GraphicalTargetConstants.ErrorTargetIdMismatch, "target_id must match the request path");
        }

        var existing = _deps.GraphicalTargets.Get(scope, targetId);
        if (existing is null)
        {
            return GraphicalError(404, GraphicalTargetConstants.ErrorNotFound, "graphical target not found");
        }

        payload.TargetId = targetId;
        payload.TenantId = existing.TenantId;
        payload.IsSystem = existing.IsSystem;
        payload.UpdatedBy = p.SubjectId;
        payload.UpdatedAt = DateTimeOffset.UtcNow;
        if (string.IsNullOrWhiteSpace(payload.DisplayName))
        {
            payload.DisplayName = existing.DisplayName;
        }

        try
        {
            var updated = _deps.GraphicalTargets.Update(scope, payload);
            return Results.Json(updated.PublicCopy(), JsonOpts);
        }
        catch (GraphicalTargetException ex)
        {
            return GraphicalRouteError(ex);
        }
    }

    private async Task<IResult> HandleDeleteGraphicalTarget(HttpContext ctx, string targetId)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!TryResolveGraphicalScope(p, "graphical.target.manage", out var scope, out var accessError))
        {
            return accessError!;
        }

        try
        {
            _deps.GraphicalTargets.Delete(scope, targetId);
        }
        catch (GraphicalTargetException ex)
        {
            return GraphicalRouteError(ex);
        }

        _ = p;
        return Results.NoContent();
    }

    private static string GenerateGraphicalTargetId()
    {
        return "gt-" + Guid.NewGuid().ToString("N")[..12];
    }

    private bool TryResolveGraphicalScope(Principal p, string capability, out GraphicalTargetScope scope, out IResult? error)
    {
        error = null;
        if (!_deps.Authz.HasCapability(p, capability))
        {
            error = DetailError(403, "graphical target access denied");
            scope = default;
            return false;
        }

        if (!GraphicalTargetScope.TryForTenant(p.TenantId ?? string.Empty, out scope))
        {
            error = DetailError(403, "graphical target access denied");
            return false;
        }

        return true;
    }

    private static IResult GraphicalRouteError(GraphicalTargetException ex)
    {
        return ex.Code switch
        {
            GraphicalTargetErrorCode.AlreadyExists => GraphicalError(409, GraphicalTargetConstants.ErrorAlreadyExists, "graphical target already exists"),
            GraphicalTargetErrorCode.Immutable => GraphicalError(409, GraphicalTargetConstants.ErrorImmutable, "static graphical target is immutable"),
            GraphicalTargetErrorCode.Conflict => GraphicalError(409, GraphicalTargetConstants.ErrorConflict, "graphical target transaction conflicted"),
            GraphicalTargetErrorCode.Invalid => GraphicalError(422, GraphicalTargetConstants.ErrorInvalidPayload, "graphical target definition is invalid"),
            GraphicalTargetErrorCode.NotFound or GraphicalTargetErrorCode.Forbidden => GraphicalError(404, GraphicalTargetConstants.ErrorNotFound, "graphical target not found"),
            GraphicalTargetErrorCode.Closed => GraphicalError(503, GraphicalTargetConstants.ErrorUnavailable, "graphical target service is unavailable"),
            _ => GraphicalError(503, GraphicalTargetConstants.ErrorBackend, "graphical target backend failed"),
        };
    }

    private static bool TryParseGraphicalTargetBody(
        Dictionary<string, JsonElement> body,
        out GraphicalTargetDefinition target,
        out bool hasTargetId,
        out bool hasTenant,
        out string? parseError)
    {
        target = new GraphicalTargetDefinition();
        hasTargetId = false;
        hasTenant = false;
        parseError = null;

        try
        {
            target.DisplayName = GetString(body, "display_name", "") ?? "";
            target.TargetId = GetString(body, "target_id", "") ?? "";
            target.Protocol = GetString(body, "protocol", GraphicalTargetConstants.ProtocolRfb)
                ?? GraphicalTargetConstants.ProtocolRfb;
            target.Endpoint = GetString(body, "endpoint", null);
            target.Secret = GetString(body, "secret", null);
            target.CaSecretRef = GetString(body, "ca_secret_ref", null);
            target.ClientCertSecretRef = GetString(body, "client_cert_secret_ref", null);
            target.ClientKeySecretRef = GetString(body, "client_key_secret_ref", null);
            target.Width = GetInt(body, "width", 640);
            target.Height = GetInt(body, "height", 480);
            target.Config = ParseConfigObject(body);
            target.TenantId = "";

            if (body.TryGetValue("tenant_id", out var tenantRaw))
            {
                hasTenant = true;
                if (tenantRaw.ValueKind is JsonValueKind.String)
                {
                    target.TenantId = tenantRaw.GetString() ?? "";
                }
            }

            hasTargetId = body.ContainsKey("target_id");

            // Semantic validation (identifier, protocol, endpoint, dimensions) is
            // performed by the registry's Create/Update, which run after the server
            // assigns the tenant + target_id. Parsing here only enforces payload
            // shape (types) so server-generated ids are not rejected prematurely.
            return true;
        }
        catch (GraphicalTargetException ex)
        {
            // Shape errors from GetString/GetInt (type mismatches). Semantic
            // validation happens later in the registry's Create/Update.
            parseError = ex.Message;
            return false;
        }
    }

    private static string? GetString(Dictionary<string, JsonElement> body, string key, string? fallback)
    {
        if (!body.TryGetValue(key, out var raw)) return fallback;
        if (raw.ValueKind == JsonValueKind.Null)
        {
            return fallback;
        }

        if (raw.ValueKind != JsonValueKind.String)
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, $"{key} must be a string");
        }

        return raw.GetString() ?? "";
    }

    // Parse the optional protocol-specific "config" object into a plain map.
    // Absent or JSON null → empty dict. A present non-object value is a shape error.
    private static Dictionary<string, object?> ParseConfigObject(Dictionary<string, JsonElement> body)
    {
        if (!body.TryGetValue("config", out var raw) || raw.ValueKind == JsonValueKind.Null)
        {
            return new Dictionary<string, object?>();
        }

        if (raw.ValueKind != JsonValueKind.Object)
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "config must be an object");
        }

        // Store the values as detached JsonElements; System.Text.Json serializes
        // them back to their original JSON shape on the wire. Keeping them opaque
        // avoids a bespoke (and mostly-unreachable) type converter.
        var map = new Dictionary<string, object?>();
        foreach (var prop in raw.EnumerateObject())
        {
            map[prop.Name] = prop.Value.Clone();
        }

        return map;
    }

    private static int GetInt(Dictionary<string, JsonElement> body, string key, int fallback)
    {
        if (!body.TryGetValue(key, out var raw)) return fallback;
        if (raw.ValueKind == JsonValueKind.Null)
        {
            return fallback;
        }

        if (raw.ValueKind == JsonValueKind.Number)
        {
            if (raw.TryGetInt32(out var n))
            {
                return n;
            }

            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, $"{key} must be an integer");
        }

        if (raw.ValueKind == JsonValueKind.String && int.TryParse(raw.GetString(), out var parsed))
        {
            return parsed;
        }

        throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, $"{key} must be an integer");
    }

    private static IResult GraphicalError(int status, string code, string message) => Results.Json(
        new { detail = new { code, message } },
        statusCode: status);
}
