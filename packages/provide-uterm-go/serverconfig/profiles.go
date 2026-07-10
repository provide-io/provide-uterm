//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// ConnectionProfile ports profiles.ConnectionProfile — a saved connection
// target owned by a principal. Times are POSIX seconds (Python time.time()).
type ConnectionProfile struct {
	ProfileID        string   `json:"profile_id"`
	Owner            string   `json:"owner"`
	Name             string   `json:"name"`
	ConnectorType    string   `json:"connector_type"`
	Host             *string  `json:"host"`
	Port             *int     `json:"port"`
	Username         *string  `json:"username"`
	Tags             []string `json:"tags"`
	InputMode        string   `json:"input_mode"`
	RecordingEnabled bool     `json:"recording_enabled"`
	Visibility       string   `json:"visibility"`
	CreatedAt        float64  `json:"created_at"`
	UpdatedAt        float64  `json:"updated_at"`
}

var profileConnectorTypes = map[string]struct{}{
	"ssh": {}, "telnet": {}, "websocket": {}, "ushell": {}, "shell": {},
}

// Validate mirrors the ConnectionProfile field Literals.
func (p *ConnectionProfile) Validate() error {
	if _, ok := profileConnectorTypes[p.ConnectorType]; !ok {
		return literalError("connector_type", "ssh", "telnet", "websocket", "ushell", "shell")
	}
	if p.InputMode == "" {
		p.InputMode = "open"
	}
	if !inSet(p.InputMode, "open", "hijack") {
		return literalError("input_mode", "open", "hijack")
	}
	if p.Visibility == "" {
		p.Visibility = "private"
	}
	if !inSet(p.Visibility, "private", "shared") {
		return literalError("visibility", "private", "shared")
	}
	if p.Tags == nil {
		p.Tags = []string{}
	}
	return nil
}

// profileMutableFields mirrors profiles._MUTABLE_FIELDS.
var profileMutableFields = map[string]struct{}{
	"name": {}, "host": {}, "port": {}, "username": {}, "tags": {},
	"input_mode": {}, "recording_enabled": {}, "visibility": {},
}

// FileProfileStore ports profiles.FileProfileStore: an atomic JSON-file-backed
// store for connection profiles, serialising concurrent access with a mutex.
type FileProfileStore struct {
	directory string
	mu        sync.Mutex
}

// NewFileProfileStore creates a store rooted at directory.
func NewFileProfileStore(directory string) *FileProfileStore {
	return &FileProfileStore{directory: directory}
}

func (s *FileProfileStore) path() string { return filepath.Join(s.directory, "profiles.json") }

// readSync reads all profiles from disk. Caller must hold the lock.
func (s *FileProfileStore) readSync() ([]ConnectionProfile, error) {
	raw, err := os.ReadFile(s.path()) //nolint:gosec // path derived from configured directory
	if err != nil {
		if os.IsNotExist(err) {
			return []ConnectionProfile{}, nil
		}
		return nil, err
	}
	var profiles []ConnectionProfile
	if err := json.Unmarshal(raw, &profiles); err != nil {
		return nil, fmt.Errorf("profiles store is corrupt at %s: %w", s.path(), err)
	}
	return profiles, nil
}

// writeSync writes all profiles atomically via temp-file + rename.
func (s *FileProfileStore) writeSync(profiles []ConnectionProfile) error {
	if err := os.MkdirAll(s.directory, 0o755); err != nil {
		return err
	}
	body, err := json.MarshalIndent(profiles, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path() + ".tmp"
	if err := os.WriteFile(tmp, body, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.path())
}

// ListProfiles returns profiles visible to owner (own + shared); when owner is
// empty, all profiles are returned.
func (s *FileProfileStore) ListProfiles(owner *string) ([]ConnectionProfile, error) {
	s.mu.Lock()
	profiles, err := s.readSync()
	s.mu.Unlock()
	if err != nil {
		return nil, err
	}
	if owner == nil {
		return profiles, nil
	}
	out := []ConnectionProfile{}
	for _, p := range profiles {
		if p.Owner == *owner || p.Visibility == "shared" {
			out = append(out, p)
		}
	}
	return out, nil
}

// GetProfile returns the profile with the given ID, or nil if not found.
func (s *FileProfileStore) GetProfile(profileID string) (*ConnectionProfile, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	profiles, err := s.readSync()
	if err != nil {
		return nil, err
	}
	for i := range profiles {
		if profiles[i].ProfileID == profileID {
			return &profiles[i], nil
		}
	}
	return nil, nil
}

// CreateProfile persists a new profile and returns it.
func (s *FileProfileStore) CreateProfile(profile ConnectionProfile) (*ConnectionProfile, error) {
	if err := profile.Validate(); err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	profiles, err := s.readSync()
	if err != nil {
		return nil, err
	}
	profiles = append(profiles, profile)
	if err := s.writeSync(profiles); err != nil {
		return nil, err
	}
	return &profile, nil
}

// UpdateProfile applies updates (restricted to the mutable field set) and
// returns the updated model, or nil if not found.
func (s *FileProfileStore) UpdateProfile(profileID string, updates map[string]any) (*ConnectionProfile, error) {
	safe := map[string]any{}
	for k, v := range updates {
		if _, ok := profileMutableFields[k]; ok {
			safe[k] = v
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	profiles, err := s.readSync()
	if err != nil {
		return nil, err
	}
	for i := range profiles {
		if profiles[i].ProfileID != profileID {
			continue
		}
		applyProfileUpdates(&profiles[i], safe)
		profiles[i].UpdatedAt = float64(time.Now().UnixNano()) / 1e9
		if err := profiles[i].Validate(); err != nil {
			return nil, err
		}
		if err := s.writeSync(profiles); err != nil {
			return nil, err
		}
		return &profiles[i], nil
	}
	return nil, nil
}

func applyProfileUpdates(p *ConnectionProfile, safe map[string]any) {
	if v, ok := safe["name"].(string); ok {
		p.Name = v
	}
	if v, ok := safe["host"]; ok {
		p.Host = optString(v)
	}
	if v, ok := safe["port"]; ok {
		if n, ok := asInt(v); ok {
			p.Port = &n
		} else {
			p.Port = nil
		}
	}
	if v, ok := safe["username"]; ok {
		p.Username = optString(v)
	}
	if v, ok := safe["tags"]; ok {
		p.Tags = asStringSlice(v)
	}
	if v, ok := safe["input_mode"].(string); ok {
		p.InputMode = v
	}
	if v, ok := safe["recording_enabled"].(bool); ok {
		p.RecordingEnabled = v
	}
	if v, ok := safe["visibility"].(string); ok {
		p.Visibility = v
	}
}

func optString(v any) *string {
	if s, ok := v.(string); ok {
		return &s
	}
	return nil
}

// DeleteProfile removes the profile. Returns true if it existed.
func (s *FileProfileStore) DeleteProfile(profileID string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	profiles, err := s.readSync()
	if err != nil {
		return false, err
	}
	kept := make([]ConnectionProfile, 0, len(profiles))
	for _, p := range profiles {
		if p.ProfileID != profileID {
			kept = append(kept, p)
		}
	}
	if len(kept) == len(profiles) {
		return false, nil
	}
	if err := s.writeSync(kept); err != nil {
		return false, err
	}
	return true, nil
}
