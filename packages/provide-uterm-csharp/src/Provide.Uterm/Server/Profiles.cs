//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Server;

/// <summary>Connection profile (Go serverconfig.ConnectionProfile / profiles.py).</summary>
public sealed class ConnectionProfile
{
    public string ProfileId { get; set; } = "";
    public string Owner { get; set; } = "";
    public string Name { get; set; } = "Unnamed";
    public string ConnectorType { get; set; } = "ssh";
    public string? Host { get; set; }
    public int? Port { get; set; }
    public string? Username { get; set; }
    public List<string> Tags { get; set; } = new();
    public string InputMode { get; set; } = "open";
    public bool RecordingEnabled { get; set; }
    public string Visibility { get; set; } = "private";
    public double CreatedAt { get; set; }
    public double UpdatedAt { get; set; }
}

public interface IProfileStore
{
    IReadOnlyList<ConnectionProfile> ListProfiles(string? ownerFilter);
    ConnectionProfile? GetProfile(string profileId);
    ConnectionProfile CreateProfile(ConnectionProfile profile);
    ConnectionProfile? UpdateProfile(string profileId, Action<ConnectionProfile> apply);
    bool DeleteProfile(string profileId);
}

public sealed class InMemoryProfileStore : IProfileStore
{
    private readonly object _gate = new();
    private readonly Dictionary<string, ConnectionProfile> _profiles = new(StringComparer.Ordinal);

    public IReadOnlyList<ConnectionProfile> ListProfiles(string? ownerFilter)
    {
        lock (_gate)
        {
            var q = _profiles.Values.AsEnumerable();
            if (ownerFilter is not null)
            {
                q = q.Where(p => p.Owner == ownerFilter);
            }

            return q.Select(Clone).ToList();
        }
    }

    public ConnectionProfile? GetProfile(string profileId)
    {
        lock (_gate)
        {
            return _profiles.TryGetValue(profileId, out var p) ? Clone(p) : null;
        }
    }

    public ConnectionProfile CreateProfile(ConnectionProfile profile)
    {
        lock (_gate)
        {
            _profiles[profile.ProfileId] = Clone(profile);
            return Clone(profile);
        }
    }

    public ConnectionProfile? UpdateProfile(string profileId, Action<ConnectionProfile> apply)
    {
        lock (_gate)
        {
            if (!_profiles.TryGetValue(profileId, out var p)) return null;
            apply(p);
            p.UpdatedAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            return Clone(p);
        }
    }

    public bool DeleteProfile(string profileId)
    {
        lock (_gate) return _profiles.Remove(profileId);
    }

    private static ConnectionProfile Clone(ConnectionProfile p) => new()
    {
        ProfileId = p.ProfileId,
        Owner = p.Owner,
        Name = p.Name,
        ConnectorType = p.ConnectorType,
        Host = p.Host,
        Port = p.Port,
        Username = p.Username,
        Tags = p.Tags.ToList(),
        InputMode = p.InputMode,
        RecordingEnabled = p.RecordingEnabled,
        Visibility = p.Visibility,
        CreatedAt = p.CreatedAt,
        UpdatedAt = p.UpdatedAt,
    };
}
