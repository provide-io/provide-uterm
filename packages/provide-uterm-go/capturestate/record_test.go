//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package capturestate

import (
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

// golden is the reference's corpus (gen_capture_record_golden.py): the files
// it writes for a live and a previous record, and the files it refuses, with
// its words.
type golden struct {
	SessionID string                     `json:"session_id"`
	Records   map[string]json.RawMessage `json:"records"`
	Refusals  []struct {
		Name  string          `json:"name"`
		Kind  string          `json:"kind"`
		Data  json.RawMessage `json:"data"`
		Error string          `json:"error"`
	} `json:"refusals"`
	Accepted []struct {
		Name        string          `json:"name"`
		Data        json.RawMessage `json:"data"`
		Session     map[string]any  `json:"session"`
		IgnoredKeys []string        `json:"ignored_keys"`
	} `json:"accepted"`
	BadFiles []struct {
		Name    string `json:"name"`
		Content []byte `json:"content_b64"`
		Error   string `json:"error"`
	} `json:"bad_files"`
}

func loadGolden(t *testing.T) golden {
	t.Helper()
	raw, err := os.ReadFile("testdata/capture_record_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g golden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

func encoded(t *testing.T, rec *Record) []byte {
	t.Helper()
	content, err := rec.encode()
	if err != nil {
		t.Fatal(err)
	}
	return content
}

func asMap(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

// A file the reference wrote reads back to the record it holds, and that
// record writes back the same file.
func TestAReferenceRecordReadsBackAndWritesTheSameFile(t *testing.T) {
	g := loadGolden(t)
	for _, kind := range []string{"live", "previous"} {
		rec, err := ParseBytes(g.Records[kind], g.SessionID, kind)
		if err != nil {
			t.Fatalf("%s: %v", kind, err)
		}
		if rec.Definition.Owner == nil || *rec.Definition.Owner != "blackbetty:7" || rec.Definition.Visibility != "private" {
			t.Fatalf("%s: definition = %+v", kind, rec.Definition)
		}
		if *rec.Screen != "\x1b[1mdeck\x1b[0m\nline two" || rec.Cols != 80 || rec.Rows != 24 {
			t.Fatalf("%s: screen = %q %dx%d", kind, *rec.Screen, rec.Cols, rec.Rows)
		}
		if !reflect.DeepEqual(rec.Scrollback, []string{"older", "old"}) || rec.SavedAt != 1760875200.5 {
			t.Fatalf("%s: scrollback %q saved_at %v", kind, rec.Scrollback, rec.SavedAt)
		}
		if (rec.EndedAt == nil) != (kind == "live") {
			t.Fatalf("%s: ended_at = %v", kind, rec.EndedAt)
		}
		if got, want := asMap(t, encoded(t, rec)), asMap(t, g.Records[kind]); !reflect.DeepEqual(got, want) {
			t.Fatalf("%s: wrote\n%v\nwant\n%v", kind, got, want)
		}
	}
}

// Every file the reference refuses is refused here, in its words. A
// definition the reference's model refuses is "bad session: " and the
// model's own complaint, which is the port's.
func TestAFileTheReferenceRefusesIsRefusedInItsWords(t *testing.T) {
	g := loadGolden(t)
	if len(g.Refusals) == 0 {
		t.Fatal("the corpus records no refusals")
	}
	for _, c := range g.Refusals {
		_, err := ParseBytes(c.Data, g.SessionID, c.Kind)
		var refused *RecordError
		if !errors.As(err, &refused) || refused.Msg != c.Error {
			t.Errorf("%s: err = %v, want %q", c.Name, err, c.Error)
		}
	}
}

// A file another build of the same format wrote is read: a key this one does
// not know is ignored, never folded into the connector's config or matched
// by case, and a missing key with a default takes the reference's.
func TestAFileAnotherBuildOfTheFormatWroteIsRead(t *testing.T) {
	g := loadGolden(t)
	if len(g.Accepted) < 3 {
		t.Fatal("the corpus records too few other builds' files")
	}
	for _, c := range g.Accepted {
		rec, err := ParseBytes(c.Data, g.SessionID, "live")
		if err != nil {
			t.Errorf("%s: %v", c.Name, err)
			continue
		}
		if got := asMap(t, encoded(t, rec))["session"]; !reflect.DeepEqual(got, any(c.Session)) {
			t.Errorf("%s: session\n%v\nwant\n%v", c.Name, got, c.Session)
		}
		if !reflect.DeepEqual(rec.IgnoredKeys, c.IgnoredKeys) {
			t.Errorf("%s: ignored %q, want %q", c.Name, rec.IgnoredKeys, c.IgnoredKeys)
		}
	}
}

func TestAMissingCreationTimeIsNow(t *testing.T) {
	g := loadGolden(t)
	var top map[string]any
	_ = json.Unmarshal(g.Records["live"], &top)
	delete(top["session"].(map[string]any), "created_at")
	data, _ := json.Marshal(top)
	before := time.Now().UTC()
	rec, err := ParseBytes(data, g.SessionID, "live")
	if err != nil || rec.Definition.CreatedAt.Before(before) {
		t.Fatalf("created_at %v, %v", rec, err)
	}
}

// Bytes that are not a JSON object at all (not UTF-8, not JSON, NaN, nested
// past the decoder's depth) are refused as the reference refuses them.
func TestAFileThatIsNotAnObjectIsRefusedAsOne(t *testing.T) {
	g := loadGolden(t)
	if len(g.BadFiles) == 0 {
		t.Fatal("the corpus records no bad files")
	}
	for _, c := range g.BadFiles {
		_, err := ParseBytes(c.Content, g.SessionID, "live")
		if err == nil || err.Error() != c.Error || c.Error != "not an object" {
			t.Errorf("%s: err = %v, want %q", c.Name, err, c.Error)
		}
	}
}

// A definition is written with lists and objects, never null, which is what
// a reader holds it to; and only a PAM capture session has a file at all.
func TestARecordIsWrittenAsItIsRead(t *testing.T) {
	g := loadGolden(t)
	rec, err := ParseBytes(g.Records["live"], g.SessionID, "live")
	if err != nil {
		t.Fatal(err)
	}
	rec.Definition.Tags, rec.Definition.ConnectorConfig = nil, nil
	content, err := rec.encode()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseBytes(content, g.SessionID, "live"); err != nil {
		t.Fatalf("its own file refused: %v", err)
	}
	rec.Definition.SessionID = "my-capture"
	if _, err := rec.encode(); err == nil || err.Error() != "not a PAM capture session: my-capture" {
		t.Fatalf("err = %v", err)
	}
}

func TestOnlyAPamCaptureIdNamesALogin(t *testing.T) {
	for id, want := range map[string][2]int64{
		"pam-suokki-capture-4242-777": {4242, 777},
		"pam-a-b-capture-1-2":         {1, 2},
	} {
		pid, ticks, ok := LoginIdentity(id)
		if !ok || int64(pid) != want[0] || ticks != want[1] {
			t.Errorf("%s: %d %d %v", id, pid, ticks, ok)
		}
	}
	for _, id := range []string{
		"pam-suokki-capture-4242-r1a2b3c4", "pam-suokki-pts-3", "pam--capture-1-2", "xpam-suokki-capture-1-2",
		"pam-suokki-capture-1-2-", "provide-shell", "pam-u-capture-99999999999999999999-1",
		"pam-u-capture-1-99999999999999999999", "pam-sü-capture-1-2", "pam-a.b-capture-1-2",
		"pam-" + strings.Repeat("u", 190) + "-capture-1-2",
	} {
		if _, _, ok := LoginIdentity(id); ok {
			t.Errorf("%s names a login", id)
		}
	}
}

func TestThePamUserIsReadFromTheID(t *testing.T) {
	for id, want := range map[string]string{"pam-suokki-capture-4242-777": "suokki", "pam-a-b-capture-1-2": "a-b"} {
		if user, ok := PamUser(id); !ok || user != want {
			t.Errorf("%s: %q %v", id, user, ok)
		}
	}
	if _, ok := PamUser("provide-shell"); ok {
		t.Error("provide-shell names a PAM user")
	}
}

func TestAnEndedRecordSummarisesItsListingFields(t *testing.T) {
	g := loadGolden(t)
	rec, err := ParseBytes(g.Records["previous"], g.SessionID, "previous")
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(rec.Summary())
	want := map[string]any{
		"id": g.SessionID, "display_name": "suokki (pts/3) [live]", "owner": "blackbetty:7",
		"ended_at": 1760875260.25, "cols": float64(80), "rows": float64(24),
	}
	if got := asMap(t, raw); !reflect.DeepEqual(got, want) {
		t.Fatalf("summary = %v", got)
	}
}

func TestARecordWithoutAScreenReadsBackWithoutOne(t *testing.T) {
	g := loadGolden(t)
	m := asMap(t, g.Records["live"])
	m["screen"] = nil
	raw, _ := json.Marshal(m)
	rec, err := ParseBytes(raw, g.SessionID, "live")
	if err != nil || rec.Screen != nil || rec.Cols != 0 || rec.Rows != 0 {
		t.Fatalf("%+v, %v", rec, err)
	}
	if got := asMap(t, encoded(t, rec))["screen"]; got != nil {
		t.Fatalf("wrote screen %v", got)
	}
}
