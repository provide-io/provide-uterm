//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Text.Json;

namespace Provide.Uterm.DeckMux;

/// <summary>Host hub capability: fan-out a DeckMux message to all browsers on a worker.</summary>
public interface IDeckMuxBroadcaster
{
    Task BroadcastAsync(string workerId, Dictionary<string, object?> msg, CancellationToken ct = default);
}

/// <summary>Mutable session presence state (scroll/pin/owner). Distinct from identity SessionPresence.</summary>
public sealed class SessionPresence
{
    public string UserId { get; set; } = "";
    public string Name { get; set; } = "";
    public string Color { get; set; } = "#4a9eff";
    public string Role { get; set; } = "viewer";
    public string Initials { get; set; } = "??";
    public int ScrollLine { get; set; }
    public object? ScrollRange { get; set; }
    public int TotalLines { get; set; }
    public Dictionary<string, object?>? Selection { get; set; }
    public Dictionary<string, object?>? Pin { get; set; }
    public bool Typing { get; set; }
    public string QueuedKeys { get; set; } = "";
    public int Cols { get; set; }
    public int Rows { get; set; }
    public bool IsOwner { get; set; }
    public double LastActivityAt { get; set; }

    public Dictionary<string, object?> ToDict() => new()
    {
        ["user_id"] = UserId,
        ["name"] = Name,
        ["color"] = Color,
        ["role"] = Role,
        ["initials"] = Initials,
        ["scroll_line"] = ScrollLine,
        ["scroll_range"] = ScrollRange ?? new List<object?> { 0, 0 },
        ["total_lines"] = TotalLines,
        ["selection"] = Selection,
        ["pin"] = Pin,
        ["typing"] = Typing,
        ["queued_keys"] = QueuedKeys,
        ["cols"] = Cols,
        ["rows"] = Rows,
        ["is_owner"] = IsOwner,
    };
}

/// <summary>Per-session presence registry (Python/Go PresenceStore).</summary>
public sealed class PresenceStore
{
    private readonly object _lock = new();
    private readonly Dictionary<string, SessionPresence> _users = new();
    private readonly List<string> _order = new();

    public int Count
    {
        get { lock (_lock) return _users.Count; }
    }

    public SessionPresence Add(string userId, string name, string color, string role, string initials)
    {
        lock (_lock)
        {
            var p = new SessionPresence
            {
                UserId = userId,
                Name = name,
                Color = color,
                Role = role,
                Initials = initials,
                LastActivityAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            };
            if (!_users.ContainsKey(userId))
            {
                _order.Add(userId);
            }

            _users[userId] = p;
            return p;
        }
    }

    public bool TryGet(string userId, out SessionPresence? user)
    {
        lock (_lock)
        {
            return _users.TryGetValue(userId, out user);
        }
    }

    public SessionPresence? GetOwner()
    {
        lock (_lock)
        {
            return _users.Values.FirstOrDefault(u => u.IsOwner);
        }
    }

    public void SetOwner(string userId)
    {
        lock (_lock)
        {
            foreach (var u in _users.Values)
            {
                u.IsOwner = u.UserId == userId;
            }
        }
    }

    public void ClearOwner()
    {
        lock (_lock)
        {
            foreach (var u in _users.Values)
            {
                u.IsOwner = false;
            }
        }
    }

    public (SessionPresence? User, bool Ok) Update(string userId, Dictionary<string, object?> fields)
    {
        lock (_lock)
        {
            if (!_users.TryGetValue(userId, out var p))
            {
                return (null, false);
            }

            foreach (var (k, v) in fields)
            {
                switch (k)
                {
                    case "scroll_line":
                        p.ScrollLine = CoerceInt(v);
                        break;
                    case "scroll_range":
                        p.ScrollRange = v;
                        break;
                    case "total_lines":
                        p.TotalLines = CoerceInt(v);
                        break;
                    case "selection":
                        p.Selection = AsDict(v);
                        break;
                    case "pin":
                        p.Pin = AsDict(v);
                        break;
                    case "typing":
                        p.Typing = v is true;
                        break;
                    case "cols":
                        p.Cols = CoerceInt(v);
                        break;
                    case "rows":
                        p.Rows = CoerceInt(v);
                        break;
                    case "queued_keys":
                        p.QueuedKeys = v?.ToString() ?? "";
                        break;
                }
            }

            p.LastActivityAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            return (p, true);
        }
    }

    public (SessionPresence? User, bool Removed) Remove(string userId)
    {
        lock (_lock)
        {
            if (!_users.Remove(userId, out var p))
            {
                return (null, false);
            }

            _order.Remove(userId);
            return (p, true);
        }
    }

    public Dictionary<string, object?> GetSyncPayload(Dictionary<string, object?>? config = null)
    {
        lock (_lock)
        {
            var users = new List<object>();
            foreach (var id in _order)
            {
                if (_users.TryGetValue(id, out var u))
                {
                    users.Add(u.ToDict());
                }
            }

            string? ownerId = _users.Values.FirstOrDefault(u => u.IsOwner)?.UserId;
            return new Dictionary<string, object?>
            {
                ["type"] = "presence_sync",
                ["users"] = users,
                ["config"] = config ?? new Dictionary<string, object?>
                {
                    ["auto_transfer_idle_s"] = 30,
                    ["keystroke_queue"] = "display",
                },
                ["owner_id"] = ownerId,
            };
        }
    }

    private static int CoerceInt(object? v) =>
        v switch
        {
            int i => i,
            long l => (int)l,
            double d => (int)d,
            JsonElement je when je.ValueKind == JsonValueKind.Number => je.TryGetInt32(out var i) ? i : 0,
            _ => 0,
        };

    private static Dictionary<string, object?>? AsDict(object? v)
    {
        if (v is null) return null;
        if (v is Dictionary<string, object?> d) return d;
        if (v is IDictionary<string, object?> id)
        {
            return id.ToDictionary(kv => kv.Key, kv => (object?)kv.Value);
        }

        if (v is JsonElement je && je.ValueKind == JsonValueKind.Object)
        {
            var outd = new Dictionary<string, object?>();
            foreach (var prop in je.EnumerateObject())
            {
                outd[prop.Name] = prop.Value.ValueKind switch
                {
                    JsonValueKind.String => prop.Value.GetString(),
                    JsonValueKind.Number => prop.Value.TryGetInt64(out var l) ? l : prop.Value.GetDouble(),
                    JsonValueKind.True => true,
                    JsonValueKind.False => false,
                    JsonValueKind.Null => null,
                    _ => prop.Value.ToString(),
                };
            }

            return outd;
        }

        return null;
    }
}

/// <summary>
/// DeckMux presence + control-transfer service (Python DeckMuxPresence / Go deckmux.DeckMuxPresence).
/// </summary>
public sealed class DeckMuxPresence
{
    private readonly IDeckMuxBroadcaster _hub;
    private readonly ConcurrentDictionary<string, PresenceStore> _stores = new();
    private readonly ConcurrentDictionary<object, string> _connUserIds = new();

    private static readonly string[] PresenceUpdateFields =
    {
        "scroll_line", "scroll_range", "total_lines", "selection", "pin", "typing", "cols", "rows",
    };

    public DeckMuxPresence(IDeckMuxBroadcaster hub) => _hub = hub;

    public PresenceStore GetStore(string workerId) =>
        _stores.GetOrAdd(workerId, _ => new PresenceStore());

    public string ResolveOrBindUserId(object ws, string? preferredId = null)
    {
        if (_connUserIds.TryGetValue(ws, out var existing))
        {
            return existing;
        }

        var id = string.IsNullOrEmpty(preferredId)
            ? "u-" + Guid.NewGuid().ToString("N")[..12]
            : preferredId!;
        _connUserIds[ws] = id;
        return id;
    }

    public async Task<Dictionary<string, object?>> OnBrowserConnectAsync(
        string workerId, object ws, string role, CancellationToken ct = default)
    {
        var store = GetStore(workerId);
        var userId = ResolveOrBindUserId(ws);
        var name = userId;
        var initials = userId.Length >= 2 ? userId[^2..].ToUpperInvariant() : "??";
        var color = IdentityNames.GenerateColor(userId, new HashSet<string>());
        store.Add(userId, name, color, role, initials);
        var sync = store.GetSyncPayload();
        sync["worker_id"] = workerId;
        if (store.Count > 1)
        {
            await _hub.BroadcastAsync(workerId, sync, ct).ConfigureAwait(false);
        }

        return sync;
    }

    public async Task OnBrowserDisconnectAsync(string workerId, object ws, CancellationToken ct = default)
    {
        if (!_connUserIds.TryRemove(ws, out var userId))
        {
            return;
        }

        var store = GetStore(workerId);
        var (_, removed) = store.Remove(userId);
        if (removed)
        {
            await _hub.BroadcastAsync(
                workerId,
                new Dictionary<string, object?>
                {
                    ["type"] = "presence_leave",
                    ["user_id"] = userId,
                },
                ct).ConfigureAwait(false);
        }
    }

    public async Task HandleMessageAsync(
        string workerId, object ws, Dictionary<string, object?> msg, CancellationToken ct = default)
    {
        var msgType = msg.TryGetValue("type", out var t) ? t?.ToString() : null;
        var store = GetStore(workerId);
        var userId = ResolveOrBindUserId(ws);

        switch (msgType)
        {
            case "presence_update":
            {
                var fields = new Dictionary<string, object?>();
                foreach (var k in PresenceUpdateFields)
                {
                    if (msg.TryGetValue(k, out var v))
                    {
                        fields[k] = v;
                    }
                }

                var (user, ok) = store.Update(userId, fields);
                if (!ok || user is null)
                {
                    return;
                }

                var updateMsg = user.ToDict();
                updateMsg["type"] = "presence_update";
                await _hub.BroadcastAsync(workerId, updateMsg, ct).ConfigureAwait(false);
                break;
            }
            case "control_request":
            {
                var owner = store.GetOwner();
                if (owner is null)
                {
                    store.SetOwner(userId);
                    await _hub.BroadcastAsync(
                        workerId,
                        new Dictionary<string, object?>
                        {
                            ["type"] = "control_transfer",
                            ["from_user_id"] = "",
                            ["to_user_id"] = userId,
                            ["reason"] = "handover",
                            ["queued_keys"] = "",
                        },
                        ct).ConfigureAwait(false);
                }
                else if (owner.UserId == userId)
                {
                    store.ClearOwner();
                    await _hub.BroadcastAsync(
                        workerId,
                        new Dictionary<string, object?>
                        {
                            ["type"] = "control_transfer",
                            ["from_user_id"] = userId,
                            ["to_user_id"] = "",
                            ["reason"] = "handover",
                            ["queued_keys"] = "",
                        },
                        ct).ConfigureAwait(false);
                }

                // else: another owner — ignore
                break;
            }
        }
    }
}
