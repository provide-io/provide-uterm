// SPDX-License-Identifier: AGPL-3.0-or-later

package memory

import (
	"context"
	"sort"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

type graphicalTargetStore struct{ state *State }

func (s *graphicalTargetStore) Put(_ context.Context, rec cp.GraphicalTargetRecord) error {
	s.state.GraphicalTargets[rec.TargetID] = rec
	return nil
}

func (s *graphicalTargetStore) Get(_ context.Context, targetID string) (*cp.GraphicalTargetRecord, error) {
	rec, ok := s.state.GraphicalTargets[targetID]
	if !ok {
		return nil, nil
	}
	return &rec, nil
}

func (s *graphicalTargetStore) List(context.Context) ([]cp.GraphicalTargetRecord, error) {
	out := make([]cp.GraphicalTargetRecord, 0, len(s.state.GraphicalTargets))
	for _, rec := range s.state.GraphicalTargets {
		out = append(out, rec)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TargetID < out[j].TargetID })
	return out, nil
}

func (s *graphicalTargetStore) Delete(_ context.Context, targetID string) (bool, error) {
	_, ok := s.state.GraphicalTargets[targetID]
	delete(s.state.GraphicalTargets, targetID)
	return ok, nil
}
