// SPDX-License-Identifier: AGPL-3.0-or-later

package controlplane

import (
	"context"
	"encoding/json"
)

// StringTuple is an immutable-by-value ordered sequence of strings. Its
// comparable representation lets the memory backend detect conflicts without
// interface-based equality or mutable slice aliasing.
type StringTuple struct{ json string }

// NewStringTuple copies values into an immutable tuple.
func NewStringTuple(values ...string) StringTuple {
	b, _ := json.Marshal(values)
	return StringTuple{json: string(b)}
}

// Values returns an independent copy of the tuple values.
func (t StringTuple) Values() []string {
	if t.json == "" {
		return []string{}
	}
	var values []string
	_ = json.Unmarshal([]byte(t.json), &values)
	return values
}

func (t StringTuple) canonicalJSON() string {
	if t.json == "" {
		return "[]"
	}
	return t.json
}

// JSON returns the tuple's deterministic compact JSON representation.
func (t StringTuple) JSON() string { return t.canonicalJSON() }

// AuditLabel is one immutable audit label pair.
type AuditLabel struct {
	Key   string
	Value string
}

// AuditLabels is an immutable-by-value ordered sequence of label pairs.
type AuditLabels struct{ json string }

// NewAuditLabels copies labels into an immutable tuple.
func NewAuditLabels(labels ...AuditLabel) AuditLabels {
	pairs := make([][2]string, len(labels))
	for i, label := range labels {
		pairs[i] = [2]string{label.Key, label.Value}
	}
	b, _ := json.Marshal(pairs)
	return AuditLabels{json: string(b)}
}

// Values returns an independent copy of the labels.
func (l AuditLabels) Values() []AuditLabel {
	if l.json == "" {
		return []AuditLabel{}
	}
	var pairs [][2]string
	_ = json.Unmarshal([]byte(l.json), &pairs)
	labels := make([]AuditLabel, len(pairs))
	for i, pair := range pairs {
		labels[i] = AuditLabel{Key: pair[0], Value: pair[1]}
	}
	return labels
}

func (l AuditLabels) canonicalJSON() string {
	if l.json == "" {
		return "[]"
	}
	return l.json
}

// JSON returns the labels' deterministic compact JSON representation.
func (l AuditLabels) JSON() string { return l.canonicalJSON() }

// GraphicalTargetStore persists transaction-bound graphical target records.
type GraphicalTargetStore interface {
	Put(context.Context, GraphicalTargetRecord) error
	Get(context.Context, string) (*GraphicalTargetRecord, error)
	List(context.Context) ([]GraphicalTargetRecord, error)
	Delete(context.Context, string) (bool, error)
}
