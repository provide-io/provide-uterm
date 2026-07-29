//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Tomlyn.Model;

namespace Provide.Uterm.ServerConfig;

/// <summary>
/// Reading one <c>[[sessions]]</c> entry, the way the reference does.
///
/// The reference is <c>config_schema_session.SessionDefinition</c>: a
/// before-validator that defaults <c>display_name</c> to <c>session_id</c> and
/// sweeps every key the model does not define into <c>connector_config</c>,
/// then field defaults for everything left unsaid. Go ports the same rules in
/// <c>serverconfig/session.go</c>.
///
/// The sweep is the part that is easy to miss and expensive to omit: it is why
/// a session may carry <c>host</c>/<c>port</c>/<c>username</c> at the top level
/// of its entry at all, and it is the reason the sessions section cannot simply
/// forbid unknown keys.
/// </summary>
internal static class SessionLoader
{
    /// <summary>
    /// Every key the reference's model defines (its <c>model_fields</c>). A key
    /// outside this set is connector config, not a mistake.
    /// </summary>
    private static readonly HashSet<string> KnownFields = new(StringComparer.Ordinal)
    {
        "session_id", "display_name", "connector_type", "connector_config",
        "input_mode", "auto_start", "tags", "recording_enabled",
        "created_at", "owner", "visibility", "ephemeral", "presence",
        "auto_transfer_idle_s", "keystroke_queue",
    };

    /// <summary>Build a definition from one decoded <c>[[sessions]]</c> table.</summary>
    public static SessionDefinition FromTable(TomlTable table)
    {
        var def = new SessionDefinition
        {
            ConnectorConfig = CollectConnectorConfig(table),
        };

        if (Str(table, "session_id") is { } sessionId) def.SessionId = sessionId.Trim();
        // display_name defaults to session_id, as the before-validator does.
        def.DisplayName = Str(table, "display_name") is { Length: > 0 } name ? name : def.SessionId;
        if (Str(table, "connector_type") is { } connectorType && connectorType.Trim().Length > 0)
        {
            def.ConnectorType = connectorType.Trim();
        }

        if (Str(table, "visibility") is { } visibility) def.Visibility = visibility;
        if (Str(table, "owner") is { } owner) def.Owner = owner;
        if (Str(table, "input_mode") is { } inputMode) def.InputMode = inputMode;
        if (Str(table, "keystroke_queue") is { } keystrokeQueue) def.KeystrokeQueue = keystrokeQueue;
        if (Bool(table, "auto_start") is { } autoStart) def.AutoStart = autoStart;
        if (Bool(table, "recording_enabled") is { } recording) def.RecordingEnabled = recording;
        if (Bool(table, "ephemeral") is { } ephemeral) def.Ephemeral = ephemeral;
        if (Bool(table, "presence") is { } presence) def.Presence = presence;
        if (table.TryGetValue("auto_transfer_idle_s", out var idle) && Int(idle) is { } idleSeconds)
        {
            def.AutoTransferIdleS = idleSeconds;
        }

        if (table.TryGetValue("tags", out var tags) && tags is TomlArray array)
        {
            def.Tags = array.OfType<string>().ToList();
        }

        return def;
    }

    /// <summary>
    /// The entry's <c>connector_config</c>, plus every key the model does not
    /// define. An explicit table is the base; a top-level extra lands on top of
    /// it, which is the order the reference's before-validator writes them in.
    /// </summary>
    private static Dictionary<string, object?> CollectConnectorConfig(TomlTable table)
    {
        var collected = new Dictionary<string, object?>(StringComparer.Ordinal);
        if (table.TryGetValue("connector_config", out var explicitConfig) && explicitConfig is TomlTable nested)
        {
            foreach (var pair in nested)
            {
                collected[pair.Key] = pair.Value;
            }
        }

        foreach (var pair in table)
        {
            if (KnownFields.Contains(pair.Key)) continue;
            collected[pair.Key] = pair.Value;
        }

        return collected;
    }

    private static string? Str(TomlTable table, string key) =>
        table.TryGetValue(key, out var value) && value is string text ? text : null;

    private static bool? Bool(TomlTable table, string key) =>
        table.TryGetValue(key, out var value) && value is bool flag ? flag : null;

    private static int? Int(object? value) => value switch
    {
        long l => (int)l,
        int i => i,
        double d => (int)d,
        _ => null,
    };
}
