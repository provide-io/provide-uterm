//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.Data.Sqlite;

namespace Provide.Uterm.ControlPlane;

/// <summary>
/// SQLite IGraphicalTargetStore — the durable half of the graphical-target
/// registry. Port of the Go controlplane/sqlite graphicalTargetStore.
/// </summary>
internal sealed class SqliteGraphicalTargetStore(SqliteEngine engine) : IGraphicalTargetStore
{
    /// <summary>
    /// The explicit column list. Spelled out rather than SELECT * so a future
    /// migration that adds a column cannot silently shift the read order.
    /// </summary>
    private const string Columns =
        "target_id, tenant_id, display_name, protocol, endpoint, secret, width, height, " +
        "is_system, is_static, ca_secret_ref, client_cert_secret_ref, client_key_secret_ref, " +
        "config, created_by, created_at, updated_by, updated_at";

    public async Task PutAsync(GraphicalTargetRecord rec, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = """
            INSERT INTO cp_graphical_targets(
                target_id, tenant_id, display_name, protocol, endpoint, secret,
                width, height, is_system, is_static, ca_secret_ref,
                client_cert_secret_ref, client_key_secret_ref, config,
                created_by, created_at, updated_by, updated_at)
            VALUES($id, $tenant, $name, $protocol, $endpoint, $secret, $width, $height,
                $isSystem, $isStatic, $ca, $cert, $key, $config, $createdBy, $createdAt,
                $updatedBy, $updatedAt)
            ON CONFLICT(target_id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                display_name = excluded.display_name,
                protocol = excluded.protocol,
                endpoint = excluded.endpoint,
                secret = excluded.secret, -- pragma: allowlist secret
                width = excluded.width,
                height = excluded.height,
                is_system = excluded.is_system,
                is_static = excluded.is_static,
                ca_secret_ref = excluded.ca_secret_ref, -- pragma: allowlist secret
                client_cert_secret_ref = excluded.client_cert_secret_ref, -- pragma: allowlist secret
                client_key_secret_ref = excluded.client_key_secret_ref, -- pragma: allowlist secret
                config = excluded.config,
                created_by = excluded.created_by,
                created_at = excluded.created_at,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """;
        cmd.Parameters.AddWithValue("$id", rec.TargetId);
        cmd.Parameters.AddWithValue("$tenant", rec.TenantId);
        cmd.Parameters.AddWithValue("$name", rec.DisplayName);
        cmd.Parameters.AddWithValue("$protocol", rec.Protocol);
        cmd.Parameters.AddWithValue("$endpoint", SqliteEngine.DbValue(rec.Endpoint));
        cmd.Parameters.AddWithValue("$secret", SqliteEngine.DbValue(rec.Secret));
        cmd.Parameters.AddWithValue("$width", rec.Width);
        cmd.Parameters.AddWithValue("$height", rec.Height);
        cmd.Parameters.AddWithValue("$isSystem", rec.IsSystem ? 1 : 0);
        cmd.Parameters.AddWithValue("$isStatic", rec.IsStatic ? 1 : 0);
        cmd.Parameters.AddWithValue("$ca", SqliteEngine.DbValue(rec.CaSecretRef));
        cmd.Parameters.AddWithValue("$cert", SqliteEngine.DbValue(rec.ClientCertSecretRef));
        cmd.Parameters.AddWithValue("$key", SqliteEngine.DbValue(rec.ClientKeySecretRef));
        cmd.Parameters.AddWithValue("$config", ConfigOrEmpty(rec.Config));
        cmd.Parameters.AddWithValue("$createdBy", SqliteEngine.DbValue(rec.CreatedBy));
        cmd.Parameters.AddWithValue("$createdAt", rec.CreatedAt);
        cmd.Parameters.AddWithValue("$updatedBy", SqliteEngine.DbValue(rec.UpdatedBy));
        cmd.Parameters.AddWithValue("$updatedAt", SqliteEngine.DbValue(rec.UpdatedAt));
        await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }

    /// <summary>
    /// Keeps the NOT NULL config column satisfied for a zero-value record: an
    /// empty string is not valid JSON, and the column default only applies when
    /// the value is omitted entirely.
    /// </summary>
    private static string ConfigOrEmpty(string config) =>
        string.IsNullOrEmpty(config) ? "{}" : config;

    private static GraphicalTargetRecord Read(SqliteDataReader r) => new()
    {
        TargetId = r.GetString(0),
        TenantId = r.GetString(1),
        DisplayName = r.GetString(2),
        Protocol = r.GetString(3),
        Endpoint = SqliteEngine.NullableString(r, 4),
        Secret = SqliteEngine.NullableString(r, 5),
        Width = r.GetInt64(6),
        Height = r.GetInt64(7),
        IsSystem = r.GetInt64(8) != 0,
        IsStatic = r.GetInt64(9) != 0,
        CaSecretRef = SqliteEngine.NullableString(r, 10),
        ClientCertSecretRef = SqliteEngine.NullableString(r, 11),
        ClientKeySecretRef = SqliteEngine.NullableString(r, 12),
        Config = r.GetString(13),
        CreatedBy = SqliteEngine.NullableString(r, 14),
        CreatedAt = r.GetDouble(15),
        UpdatedBy = SqliteEngine.NullableString(r, 16),
        UpdatedAt = SqliteEngine.NullableDouble(r, 17),
    };

    public async Task<GraphicalTargetRecord?> GetAsync(string targetId, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = $"SELECT {Columns} FROM cp_graphical_targets WHERE target_id = $id";
        cmd.Parameters.AddWithValue("$id", targetId);
        await using var r = await cmd.ExecuteReaderAsync(ct).ConfigureAwait(false);
        return await r.ReadAsync(ct).ConfigureAwait(false) ? Read(r) : null;
    }

    public async Task<IReadOnlyList<GraphicalTargetRecord>> ListAsync(CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = $"SELECT {Columns} FROM cp_graphical_targets ORDER BY target_id";
        await using var r = await cmd.ExecuteReaderAsync(ct).ConfigureAwait(false);
        var list = new List<GraphicalTargetRecord>();
        while (await r.ReadAsync(ct).ConfigureAwait(false))
        {
            list.Add(Read(r));
        }

        return list;
    }

    public async Task<bool> DeleteAsync(string targetId, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = "DELETE FROM cp_graphical_targets WHERE target_id = $id";
        cmd.Parameters.AddWithValue("$id", targetId);
        return await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false) > 0;
    }
}
