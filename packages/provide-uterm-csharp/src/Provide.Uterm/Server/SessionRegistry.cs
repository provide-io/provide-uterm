//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json.Serialization;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Server;

/// <summary>
/// The names a session's lifecycle can go by — the reference's vocabulary and
/// no other: <c>bridge/contracts.py</c> declares
/// <c>SessionLifecycle = Literal["stopped", "starting", "running", "error"]</c>
/// and <c>server/runtime.py</c> is what assigns them.
///
/// A name outside this set is a name no dashboard, no client and no other port
/// knows how to read, however sensible it sounds on its own.
/// </summary>
public static class SessionLifecycleState
{
    /// <summary>Registered but not brought up, or brought down again.</summary>
    public const string Stopped = "stopped";

    /// <summary>Asked to come up; the connector has not reported in yet.</summary>
    public const string Starting = "starting";

    /// <summary>Up.</summary>
    public const string Running = "running";

    /// <summary>The connector failed; <c>last_error</c> says how.</summary>
    public const string Error = "error";

    /// <summary>Every name, for validating what a port reports.</summary>
    public static readonly IReadOnlyList<string> All = [Stopped, Starting, Running, Error];
}

/// <summary>
/// Runtime session status returned by /api/sessions — the wire shape of
/// Python's <c>SessionRuntimeStatus.model_dump</c> and Go's
/// <c>server.SessionStatus</c>, property for property and in their order.
///
/// The three nullable fields opt out of the server's global
/// "omit when null" so they serialize as JSON <c>null</c>: a client has to be
/// able to tell "has not stopped" from "this port does not say".
///
/// <see cref="IsHijacked"/> and <see cref="ConnectorConfig"/> are runtime
/// bookkeeping this port keeps on the same object; neither is part of the
/// reference wire shape, so neither is serialized.
/// </summary>
public sealed class SessionStatus
{
    public required string SessionId { get; set; }
    public required string DisplayName { get; set; }
    public string CreatedAt { get; set; } = Timestamps.NowIso();
    public required string ConnectorType { get; set; }
    public string LifecycleState { get; set; } = SessionLifecycleState.Stopped;
    public string InputMode { get; set; } = "open";
    public bool Connected { get; set; }
    public bool AutoStart { get; set; } = true;
    public List<string> Tags { get; set; } = new();
    public bool RecordingEnabled { get; set; }
    public bool RecordingAvailable { get; set; }

    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public string? Owner { get; set; }

    public required string Visibility { get; set; }

    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public double? StoppedAt { get; set; }

    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public string? LastError { get; set; }

    /// <summary>Whether a hijack lease is held. Hub state, not wire state.</summary>
    [JsonIgnore]
    public bool IsHijacked { get; set; }

    /// <summary>Connector config used at connect time. Hub state, not wire state.</summary>
    [JsonIgnore]
    public Dictionary<string, object?> ConnectorConfig { get; set; } = new();
}

/// <summary>
/// The one place this port formats a wall-clock instant for the API.
///
/// The reference emits Python's <c>datetime.isoformat</c> of a UTC value —
/// microsecond precision and a <c>Z</c> suffix. .NET's round-trip format
/// ("O") writes seven fractional digits and <c>+00:00</c>, which is the same
/// instant but not the same string, so it is not used here.
/// </summary>
public static class Timestamps
{
    /// <summary>Now, as the reference writes it.</summary>
    public static string NowIso() => Iso(DateTimeOffset.UtcNow);

    /// <summary>An instant, as the reference writes it.</summary>
    public static string Iso(DateTimeOffset when) =>
        when.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.ffffff") + "Z";
}

public sealed class SessionItem
{
    public SessionDefinition? Definition { get; set; }
    public SessionStatus? Status { get; set; }
}

public interface ISessionRegistry
{
    bool TryGetDefinition(string sessionId, out SessionDefinition definition);
    bool TryGetStatus(string sessionId, out SessionStatus status);
    IReadOnlyList<SessionItem> ListWithDefinitions();
    SessionDefinition Upsert(SessionDefinition def);
    bool Delete(string sessionId);
    /// <summary>Lifecycle: stopped → running.</summary>
    SessionStatus? StartSession(string sessionId);
    /// <summary>Lifecycle: running → stopped.</summary>
    SessionStatus? StopSession(string sessionId);
    SessionStatus? RestartSession(string sessionId);
    SessionStatus? ClearSession(string sessionId);
    SessionStatus? SetMode(string sessionId, string inputMode);
    /// <summary>Partial update of definition + status display fields.</summary>
    SessionStatus? PatchSession(string sessionId, string? displayName, string? visibility, IReadOnlyList<string>? tags);
    /// <summary>Admin bulk delete by lifecycle state (optional).</summary>
    int BulkDelete(string? lifecycleState);
}

/// <summary>In-memory session registry seeded from config sessions.</summary>
public sealed class InMemorySessionRegistry : ISessionRegistry
{
    private readonly object _gate = new();
    private readonly Dictionary<string, SessionDefinition> _defs = new(StringComparer.Ordinal);
    private readonly Dictionary<string, SessionStatus> _status = new(StringComparer.Ordinal);
    private readonly bool _recordingEnabledByDefault;

    /// <param name="seed">Config-declared sessions to register at boot.</param>
    /// <param name="recordingEnabledByDefault">
    /// What a definition that states no preference resolves to —
    /// <c>recording.enabled_by_default</c>, as in the reference.
    /// </param>
    public InMemorySessionRegistry(
        IEnumerable<SessionDefinition>? seed = null, bool recordingEnabledByDefault = false)
    {
        _recordingEnabledByDefault = recordingEnabledByDefault;
        if (seed is null) return;
        foreach (var def in seed)
        {
            if (string.IsNullOrWhiteSpace(def.SessionId)) continue;
            Upsert(def);
        }
    }

    public bool TryGetDefinition(string sessionId, out SessionDefinition definition)
    {
        lock (_gate)
        {
            if (_defs.TryGetValue(sessionId, out var d))
            {
                definition = d;
                return true;
            }

            definition = null!;
            return false;
        }
    }

    public bool TryGetStatus(string sessionId, out SessionStatus status)
    {
        lock (_gate)
        {
            if (_status.TryGetValue(sessionId, out var st))
            {
                status = st;
                return true;
            }

            status = null!;
            return false;
        }
    }

    public IReadOnlyList<SessionItem> ListWithDefinitions()
    {
        lock (_gate)
        {
            return _defs.Keys.Select(id => new SessionItem
            {
                Definition = _defs[id],
                Status = _status.TryGetValue(id, out var st) ? st : null,
            }).ToList();
        }
    }

    public SessionStatus? StartSession(string sessionId)
    {
        lock (_gate)
        {
            if (!_status.TryGetValue(sessionId, out var st)) return null;
            st.LifecycleState = SessionLifecycleState.Running;
            st.Connected = true;
            st.StoppedAt = null;
            return CloneStatus(st);
        }
    }

    public SessionStatus? StopSession(string sessionId)
    {
        lock (_gate)
        {
            if (!_status.TryGetValue(sessionId, out var st)) return null;
            st.LifecycleState = SessionLifecycleState.Stopped;
            st.Connected = false;
            st.IsHijacked = false;
            st.StoppedAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            return CloneStatus(st);
        }
    }

    public SessionStatus? RestartSession(string sessionId)
    {
        lock (_gate)
        {
            if (!_status.TryGetValue(sessionId, out var st)) return null;
            st.LifecycleState = SessionLifecycleState.Running;
            st.Connected = true;
            st.IsHijacked = false;
            st.StoppedAt = null;
            return CloneStatus(st);
        }
    }

    public SessionStatus? ClearSession(string sessionId)
    {
        lock (_gate)
        {
            if (!_status.TryGetValue(sessionId, out var st)) return null;
            // Clear does not stop the session; marks a soft reset.
            st.IsHijacked = false;
            return CloneStatus(st);
        }
    }

    public SessionStatus? SetMode(string sessionId, string inputMode)
    {
        lock (_gate)
        {
            if (!_status.TryGetValue(sessionId, out var st)) return null;
            st.InputMode = inputMode;
            return CloneStatus(st);
        }
    }

    public SessionStatus? PatchSession(
        string sessionId, string? displayName, string? visibility, IReadOnlyList<string>? tags)
    {
        lock (_gate)
        {
            if (!_defs.TryGetValue(sessionId, out var def) || !_status.TryGetValue(sessionId, out var st))
            {
                return null;
            }

            if (displayName is not null)
            {
                def.DisplayName = displayName;
                st.DisplayName = displayName;
            }

            if (visibility is not null)
            {
                def.Visibility = visibility;
                st.Visibility = visibility;
            }

            if (tags is not null)
            {
                def.Tags = tags.ToList();
                st.Tags = tags.ToList();
            }

            return CloneStatus(st);
        }
    }

    public int BulkDelete(string? lifecycleState)
    {
        lock (_gate)
        {
            var ids = _status
                .Where(kv => lifecycleState is null || kv.Value.LifecycleState == lifecycleState)
                .Select(kv => kv.Key)
                .ToList();
            foreach (var id in ids)
            {
                _status.Remove(id);
                _defs.Remove(id);
            }

            return ids.Count;
        }
    }

    private static SessionStatus CloneStatus(SessionStatus st) => new()
    {
        SessionId = st.SessionId,
        DisplayName = st.DisplayName,
        CreatedAt = st.CreatedAt,
        ConnectorType = st.ConnectorType,
        LifecycleState = st.LifecycleState,
        InputMode = st.InputMode,
        Connected = st.Connected,
        AutoStart = st.AutoStart,
        Tags = st.Tags.ToList(),
        RecordingEnabled = st.RecordingEnabled,
        RecordingAvailable = st.RecordingAvailable,
        Owner = st.Owner,
        Visibility = st.Visibility,
        StoppedAt = st.StoppedAt,
        LastError = st.LastError,
        IsHijacked = st.IsHijacked,
        ConnectorConfig = new Dictionary<string, object?>(st.ConnectorConfig),
    };

    public SessionDefinition Upsert(SessionDefinition def)
    {
        lock (_gate)
        {
            _defs[def.SessionId] = def;
            var recording = def.RecordingEnabled ?? _recordingEnabledByDefault;
            if (!_status.ContainsKey(def.SessionId))
            {
                _status[def.SessionId] = new SessionStatus
                {
                    SessionId = def.SessionId,
                    DisplayName = string.IsNullOrEmpty(def.DisplayName) ? def.SessionId : def.DisplayName,
                    CreatedAt = Timestamps.NowIso(),
                    ConnectorType = def.ConnectorType,
                    LifecycleState = SessionLifecycleState.Stopped,
                    InputMode = def.InputMode,
                    AutoStart = def.AutoStart,
                    Tags = def.Tags.ToList(),
                    RecordingEnabled = recording,
                    RecordingAvailable = recording,
                    Owner = def.Owner,
                    Visibility = def.Visibility,
                };
            }
            else
            {
                var st = _status[def.SessionId];
                st.DisplayName = string.IsNullOrEmpty(def.DisplayName) ? def.SessionId : def.DisplayName;
                st.ConnectorType = def.ConnectorType;
                st.Visibility = def.Visibility;
                st.Owner = def.Owner;
                st.Tags = def.Tags.ToList();
                st.AutoStart = def.AutoStart;
                st.RecordingEnabled = recording;
                st.RecordingAvailable = recording;
            }

            return def;
        }
    }

    public bool Delete(string sessionId)
    {
        lock (_gate)
        {
            _status.Remove(sessionId);
            return _defs.Remove(sessionId);
        }
    }

    public void MarkWorker(string sessionId, bool online, bool isHijacked, string inputMode)
    {
        lock (_gate)
        {
            if (!_status.TryGetValue(sessionId, out var st)) return;
            st.Connected = online;
            st.IsHijacked = isHijacked;
            st.InputMode = inputMode;
            st.LifecycleState = online ? SessionLifecycleState.Running : SessionLifecycleState.Stopped;
        }
    }
}
