//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Server;

/// <summary>Runtime session status returned by /api/sessions.</summary>
public sealed class SessionStatus
{
    public required string SessionId { get; set; }
    public required string DisplayName { get; set; }
    public required string ConnectorType { get; set; }
    public required string Visibility { get; set; }
    public string LifecycleState { get; set; } = "created";
    public string CreatedAt { get; set; } = DateTimeOffset.UtcNow.ToString("O");
    public string? Owner { get; set; }
    public List<string> Tags { get; set; } = new();
    public bool WorkerOnline { get; set; }
    public bool IsHijacked { get; set; }
    public string InputMode { get; set; } = "hijack";
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
    /// <summary>Lifecycle: created/disconnected → running.</summary>
    SessionStatus? StartSession(string sessionId);
    /// <summary>Lifecycle: running → disconnected.</summary>
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

    public InMemorySessionRegistry(IEnumerable<SessionDefinition>? seed = null)
    {
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
            st.LifecycleState = "running";
            st.WorkerOnline = true;
            return CloneStatus(st);
        }
    }

    public SessionStatus? StopSession(string sessionId)
    {
        lock (_gate)
        {
            if (!_status.TryGetValue(sessionId, out var st)) return null;
            st.LifecycleState = "disconnected";
            st.WorkerOnline = false;
            st.IsHijacked = false;
            return CloneStatus(st);
        }
    }

    public SessionStatus? RestartSession(string sessionId)
    {
        lock (_gate)
        {
            if (!_status.TryGetValue(sessionId, out var st)) return null;
            st.LifecycleState = "running";
            st.WorkerOnline = true;
            st.IsHijacked = false;
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
        ConnectorType = st.ConnectorType,
        Visibility = st.Visibility,
        LifecycleState = st.LifecycleState,
        CreatedAt = st.CreatedAt,
        Owner = st.Owner,
        Tags = st.Tags.ToList(),
        WorkerOnline = st.WorkerOnline,
        IsHijacked = st.IsHijacked,
        InputMode = st.InputMode,
    };

    public SessionDefinition Upsert(SessionDefinition def)
    {
        lock (_gate)
        {
            _defs[def.SessionId] = def;
            if (!_status.ContainsKey(def.SessionId))
            {
                _status[def.SessionId] = new SessionStatus
                {
                    SessionId = def.SessionId,
                    DisplayName = string.IsNullOrEmpty(def.DisplayName) ? def.SessionId : def.DisplayName,
                    ConnectorType = def.ConnectorType,
                    Visibility = def.Visibility,
                    Owner = def.Owner,
                    Tags = def.Tags.ToList(),
                    LifecycleState = "created",
                    CreatedAt = DateTimeOffset.UtcNow.ToString("O"),
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
            st.WorkerOnline = online;
            st.IsHijacked = isHijacked;
            st.InputMode = inputMode;
            st.LifecycleState = online ? "running" : "disconnected";
        }
    }
}
