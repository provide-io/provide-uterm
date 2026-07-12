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
    IReadOnlyList<SessionItem> ListWithDefinitions();
    SessionDefinition Upsert(SessionDefinition def);
    bool Delete(string sessionId);
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
