//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package capturestate

import "errors"

// ErrNotSaved is an update whose new file could not be written. The old file
// and the listing are as they were; the write has logged why.
var ErrNotSaved = errors.New("previous session not saved")

// UpdatePrevious sets owner and/or visibility (nil leaves a field as it is)
// on id's previous record and returns its listing copy, without the screen.
// Port of CaptureStateStore.update_previous.
//
// The file is read, changed and rewritten whole, under the session's own
// lock, so the screen it holds is kept and no other write of the session's
// files interleaves with it. The result is nil, with no error, when the
// record is not listed, when its file has gone or turned unreadable since
// (its listing entry is dropped, since nobody can read it any more, but the
// file is not removed), or when the new owner's cap removed this very record.
// The values are the caller's to have judged.
func (s *Store) UpdatePrevious(id string, owner, visibility *string) (*Record, error) {
	w := s.writer(id)
	w.Lock()
	defer w.Unlock()
	rec, ok := s.PreviousSession(id)
	if !ok {
		s.mu.Lock()
		delete(s.previous, id)
		s.mu.Unlock()
		return nil, nil
	}
	if owner != nil {
		rec.Definition.Owner = owner
	}
	if visibility != nil {
		rec.Definition.Visibility = *visibility
	}
	if !s.write(s.previousDir, rec) {
		return nil, ErrNotSaved
	}
	s.mu.Lock()
	s.previous[id] = withoutScreen(rec)
	dropped := s.pruneOwnerLocked(ownerKey(rec))
	kept := s.previous[id]
	s.mu.Unlock()
	s.removePrevious(dropped)
	return kept, nil
}
