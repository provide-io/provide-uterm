//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/capturestate"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// PATCH /api/previous-sessions/{id}: who owns an ended session, and who else
// may read it. Port of the PATCH half of test_previous_sessions.py and
// test_previous_sessions_update.py.

var rootAdmin = as("root", "admin")

func patchPrevious(ts *testServer, id, body string, headers map[string]string) (int, map[string]any) {
	rec := ts.do("PATCH", "/api/previous-sessions/"+id, body, headers)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func onDisk(t *testing.T, dir, id string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, "previous", id+".json"))
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func listingEntry(t *testing.T, ts *testServer, id string) map[string]any {
	t.Helper()
	rec := ts.do("GET", "/api/previous-sessions", "", rootAdmin)
	var body struct {
		Sessions []map[string]any `json:"sessions"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	for _, s := range body.Sessions {
		if s["id"] == id {
			return s
		}
	}
	return nil
}

// reopen is the store a restart builds over the same directory.
func reopen(t *testing.T, dir string) *capturestate.Store {
	t.Helper()
	store := capturestate.New(dir, false, 7, 100, slog.New(slog.NewTextHandler(io.Discard, nil)))
	store.LoadPrevious()
	return store
}

func TestAnAdminSetsTheOwnerAndVisibility(t *testing.T) {
	ts, dir := previousServer(t, 7)
	before := listingEntry(t, ts, bobPublic)
	code, body := patchPrevious(ts, bobPublic, `{"owner":"blackbetty:7","visibility":"private"}`, rootAdmin)
	// The listing's entry, as the list returns it: no screen.
	want := map[string]any{}
	for k, v := range before {
		want[k] = v
	}
	want["owner"] = "blackbetty:7"
	if code != http.StatusOK || !reflect.DeepEqual(body, want) {
		t.Fatalf("%d %v, want %v", code, body, want)
	}
	file := onDisk(t, dir, bobPublic)
	session := file["session"].(map[string]any)
	if session["owner"] != "blackbetty:7" || session["visibility"] != "private" {
		t.Fatalf("session %v", session)
	}
	// Everything else is kept, the screen and when it ended above all.
	if file["screen"].(map[string]any)["ansi"] != "bob's public screen" || file["ended_at"] != before["ended_at"] ||
		!reflect.DeepEqual(file["scrollback"], []any{"one", "two"}) {
		t.Fatalf("file %v", file)
	}
	// The new owner reads it; its old owner, and an operator now that it is private, do not.
	if got := listed(t, ts, as("blackbetty:7", "viewer")); !reflect.DeepEqual(got, []string{bobPublic}) {
		t.Fatalf("new owner lists %v", got)
	}
	read := ts.do("GET", "/api/previous-sessions/"+bobPublic, "", as("blackbetty:7", "viewer"))
	if read.Code != http.StatusOK || decode(t, read.Body.Bytes())["screen"] != "bob's public screen" {
		t.Fatalf("new owner reads %d %s", read.Code, read.Body.String())
	}
	if got := listed(t, ts, as("bob", "viewer")); !reflect.DeepEqual(got, []string{bobPrivate}) {
		t.Fatalf("old owner lists %v", got)
	}
	if got := listed(t, ts, as("carol", "operator")); !reflect.DeepEqual(got, []string{aliceOperator}) {
		t.Fatalf("operator lists %v", got)
	}
}

func TestAnOwnerSetSurvivesARestart(t *testing.T) {
	ts, dir := previousServer(t, 7)
	if code, _ := patchPrevious(ts, alicePrivate, `{"owner":"blackbetty:7"}`, rootAdmin); code != http.StatusOK {
		t.Fatalf("patch %d", code)
	}
	store := reopen(t, dir)
	kept, ok := store.PreviousSummary(alicePrivate)
	if !ok || *kept.Definition.Owner != "blackbetty:7" || kept.Definition.Visibility != "private" {
		t.Fatalf("reloaded %+v", kept)
	}
	if full, ok := store.PreviousSession(alicePrivate); !ok || *full.Screen != "alice's private screen" {
		t.Fatalf("screen %+v", full)
	}
}

func TestOneFieldLeavesTheOtherAsItWas(t *testing.T) {
	ts, dir := previousServer(t, 7)
	if code, _ := patchPrevious(ts, aliceOperator, `{"visibility":"public"}`, rootAdmin); code != http.StatusOK {
		t.Fatalf("patch %d", code)
	}
	session := onDisk(t, dir, aliceOperator)["session"].(map[string]any)
	if session["owner"] != "alice" || session["visibility"] != "public" {
		t.Fatalf("session %v", session)
	}
	if got := listed(t, ts, as("carol", "operator")); !reflect.DeepEqual(got, []string{bobPublic, aliceOperator}) {
		t.Fatalf("operator lists %v", got)
	}
	code, body := patchPrevious(ts, aliceOperator, `{"owner":"blackbetty:7"}`, rootAdmin)
	if code != http.StatusOK || body["owner"] != "blackbetty:7" {
		t.Fatalf("%d %v", code, body)
	}
	if onDisk(t, dir, aliceOperator)["session"].(map[string]any)["visibility"] != "public" {
		t.Fatal("visibility changed")
	}
}

func TestAChangeIsAuditedAndARefusedOneIsNot(t *testing.T) {
	var logs bytes.Buffer
	ts, _ := previousServer(t, 7, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Logger = slog.New(slog.NewTextHandler(&logs, nil))
	})
	patchPrevious(ts, alicePrivate, `{"owner":"blackbetty:7","visibility":"operator"}`, rootAdmin)
	patchPrevious(ts, alicePrivate, `{"owner":"blackbetty:7"}`, as("alice", "viewer"))
	patchPrevious(ts, alicePrivate, `{"colour":"red"}`, rootAdmin)
	text := logs.String()
	if strings.Count(text, "previous_session.update") != 1 || !strings.Contains(text, "principal=root") ||
		!strings.Contains(text, "session_id:"+alicePrivate) || !strings.Contains(text, "owner:blackbetty:7") {
		t.Fatalf("audit log %s", text)
	}
}

func TestAnyoneButAnAdminIsRefusedAndNothingChanges(t *testing.T) {
	ts, dir := previousServer(t, 7)
	before := onDisk(t, dir, alicePrivate)
	for _, headers := range []map[string]string{
		as("alice", "viewer"), as("carol", "operator"), as("dave", "viewer"),
		{"X-Subject": "scoped", "X-Role": "admin", "X-Admin-Session-Scope": alicePrivate},
	} {
		for _, body := range []string{`{"owner":"alice"}`, `{"visibility":"public"}`, `{"owner":"x","visibility":"public"}`} {
			code, out := patchPrevious(ts, alicePrivate, body, headers)
			if code != http.StatusForbidden || out["detail"] != "admin privileges required" {
				t.Errorf("%v %s: %d %v", headers, body, code, out)
			}
		}
	}
	if !reflect.DeepEqual(onDisk(t, dir, alicePrivate), before) {
		t.Error("the file changed")
	}
	if got := listed(t, ts, as("alice", "viewer")); !reflect.DeepEqual(got, []string{aliceOperator, alicePrivate}) {
		t.Errorf("alice lists %v", got)
	}
}

func TestAShareViewerIsRefusedWhateverItsRoles(t *testing.T) {
	ts, dir := previousServer(t, 7)
	before := onDisk(t, dir, alicePrivate)
	for _, subject := range []string{"share:" + alicePrivate + ":blackbetty:7", "share:" + alicePrivate + ":viewer"} {
		for _, role := range []string{"viewer", "operator", "admin"} {
			code, out := patchPrevious(ts, alicePrivate, `{"owner":"blackbetty:7"}`, as(subject, role))
			if code != http.StatusForbidden || out["detail"] != "admin privileges required" {
				t.Errorf("%s as %s: %d %v", subject, role, code, out)
			}
		}
	}
	if !reflect.DeepEqual(onDisk(t, dir, alicePrivate), before) {
		t.Error("the file changed")
	}
}

func TestPatchingAnUnknownOrUnkeptSessionIs404(t *testing.T) {
	unknown := "pam-alice-capture-9-9"
	ts, _ := previousServer(t, 7)
	code, out := patchPrevious(ts, unknown, `{"owner":"x"}`, rootAdmin)
	if code != http.StatusNotFound || out["detail"] != "unknown previous session: "+unknown {
		t.Fatalf("unknown: %d %v", code, out)
	}
	// Unknown before the caller is judged: it learns nothing of what was kept.
	if code, _ := patchPrevious(ts, unknown, `{"owner":"x"}`, as("dave", "viewer")); code != http.StatusNotFound {
		t.Fatalf("unknown as a non-admin: %d", code)
	}
	off, _ := previousServer(t, 0)
	if code, out := patchPrevious(off, alicePrivate, `{"owner":"x"}`, rootAdmin); code != http.StatusNotFound ||
		out["detail"] != "unknown previous session: "+alicePrivate {
		t.Fatalf("off: %d %v", code, out)
	}
	none := newTestServer(t, nil)
	if code, _ := patchPrevious(none, alicePrivate, `{"owner":"x"}`, rootAdmin); code != http.StatusNotFound {
		t.Fatalf("no store: %d", code)
	}
}

func TestPatchingNeedsACallerAndASafeId(t *testing.T) {
	ts, _ := previousServer(t, 7)
	if code, _ := patchPrevious(ts, alicePrivate, `{"owner":"x"}`, nil); code != http.StatusUnauthorized {
		t.Fatalf("anonymous: %d", code)
	}
	if code, _ := patchPrevious(ts, "a.b", `{"owner":"x"}`, rootAdmin); code != http.StatusUnprocessableEntity {
		t.Fatalf("unsafe id: %d", code)
	}
}

func TestABodyThatIsNotAnOwnerOrVisibilityIs422(t *testing.T) {
	nothingElse := "a previous session takes owner and/or visibility, nothing else"
	ts, dir := previousServer(t, 7)
	before := onDisk(t, dir, alicePrivate)
	for _, tc := range []struct{ body, detail string }{
		{`{}`, nothingElse},
		{`{"owner":"x","display_name":"y"}`, nothingElse},
		{`{"ended_at":1.0}`, nothingElse},
		{`{"owner":null}`, "owner must be a string"},
		{`{"owner":7}`, "owner must be a string"},
		{`{"owner":"x","visibility":"secret"}`, "invalid visibility for " + alicePrivate + ": 'secret'"},
		{`{"visibility":null}`, "invalid visibility for " + alicePrivate + ": None"},
		{`{"visibility":7}`, "invalid visibility for " + alicePrivate + ": 7"},
		{`{"visibility":true}`, "invalid visibility for " + alicePrivate + ": True"},
		{`{"visibility":"it's"}`, `invalid visibility for ` + alicePrivate + `: "it's"`},
		{`["owner"]`, "invalid request body"},
		{`{"owner":`, "invalid request body"},
		{``, "invalid request body"},
	} {
		code, out := patchPrevious(ts, alicePrivate, tc.body, rootAdmin)
		if code != http.StatusUnprocessableEntity || out["detail"] != tc.detail {
			t.Errorf("%q: %d %v, want %q", tc.body, code, out, tc.detail)
		}
	}
	if !reflect.DeepEqual(onDisk(t, dir, alicePrivate), before) {
		t.Error("the file changed")
	}
}

func TestAFileGoneOrUnreadableSinceListingIs404AndUnlisted(t *testing.T) {
	for _, damage := range []string{"unlink", "garble"} {
		ts, dir := previousServer(t, 7)
		path := filepath.Join(dir, "previous", alicePrivate+".json")
		if damage == "unlink" {
			_ = os.Remove(path)
		} else {
			_ = os.WriteFile(path, []byte("{not json"), 0o600)
		}
		code, out := patchPrevious(ts, alicePrivate, `{"owner":"blackbetty:7"}`, rootAdmin)
		if code != http.StatusNotFound || out["detail"] != "unknown previous session: "+alicePrivate {
			t.Errorf("%s: %d %v", damage, code, out)
		}
		if listingEntry(t, ts, alicePrivate) != nil {
			t.Errorf("%s: still listed", damage)
		}
		// Nothing is written in its place.
		if _, err := os.Stat(path); (err == nil) != (damage == "garble") {
			t.Errorf("%s: file present = %v", damage, err == nil)
		}
	}
}

func TestAWriteThatFailsIs500AndChangesNothing(t *testing.T) {
	ts, dir := previousServer(t, 7)
	previous := filepath.Join(dir, "previous")
	before := onDisk(t, dir, alicePrivate)
	if err := os.Chmod(previous, 0o500); err != nil {
		t.Fatal(err)
	}
	code, out := patchPrevious(ts, alicePrivate, `{"owner":"blackbetty:7"}`, rootAdmin)
	_ = os.Chmod(previous, 0o700)
	if code != http.StatusInternalServerError || out["detail"] != "previous session not saved" {
		t.Fatalf("%d %v", code, out)
	}
	if !reflect.DeepEqual(onDisk(t, dir, alicePrivate), before) {
		t.Error("the file changed")
	}
	if got := listed(t, ts, as("alice", "viewer")); !reflect.DeepEqual(got, []string{aliceOperator, alicePrivate}) {
		t.Errorf("alice lists %v", got)
	}
}

// crowded is the previous sessions server with twenty of blackbetty:7's,
// ended from to to seconds ago, the first the newest.
func crowded(t *testing.T, from, to float64) (*testServer, string) {
	return previousServer(t, 7, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		for i := 0; i < 20; i++ {
			id := "pam-carl-capture-" + strconv.Itoa(i) + "-10"
			endPrevious(deps.CaptureState, id, "blackbetty:7", "private", "s", from+(to-from)*float64(i)/19)
		}
	})
}

func TestTheNewOwnersCapDropsItsOldest(t *testing.T) {
	// alicePrivate ended 30 s ago; the twenty ended 100 to 119 s ago.
	ts, dir := crowded(t, 100, 119)
	if code, _ := patchPrevious(ts, alicePrivate, `{"owner":"blackbetty:7"}`, rootAdmin); code != http.StatusOK {
		t.Fatalf("patch %d", code)
	}
	want := []string{alicePrivate}
	for i := 0; i < 19; i++ {
		want = append(want, "pam-carl-capture-"+strconv.Itoa(i)+"-10")
	}
	if got := listed(t, ts, as("blackbetty:7", "viewer")); !reflect.DeepEqual(got, want) {
		t.Fatalf("lists %v", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "previous", "pam-carl-capture-19-10.json")); err == nil {
		t.Error("the dropped one's file stayed")
	}
}

func TestOneTheNewOwnersCapDropsIs404(t *testing.T) {
	// The twenty ended 1 to 20 s ago, all after alicePrivate's 30 s.
	ts, dir := crowded(t, 1, 20)
	code, out := patchPrevious(ts, alicePrivate, `{"owner":"blackbetty:7"}`, rootAdmin)
	if code != http.StatusNotFound || out["detail"] != "unknown previous session: "+alicePrivate {
		t.Fatalf("%d %v", code, out)
	}
	if got := listed(t, ts, as("blackbetty:7", "viewer")); len(got) != 20 {
		t.Fatalf("lists %d", len(got))
	}
	if _, err := os.Stat(filepath.Join(dir, "previous", alicePrivate+".json")); err == nil {
		t.Error("the evicted one's file stayed")
	}
}

func TestThePatchRouteIsOnlyPatch(t *testing.T) {
	ts, _ := previousServer(t, 7)
	for _, method := range []string{"PUT", "POST", "DELETE"} {
		if rec := ts.do(method, "/api/previous-sessions/"+alicePrivate, `{"owner":"x"}`, rootAdmin); rec.Code != http.StatusMethodNotAllowed {
			t.Errorf("%s: %d", method, rec.Code)
		}
	}
}

// A refusal quotes the value as the reference's repr() writes it.
func TestPythonReprQuotesAsPythonDoes(t *testing.T) {
	for in, want := range map[string]string{
		"plain": `'plain'`, "it's": `"it's"`, `say "hi"`: `'say "hi"'`, `both ' and "`: `'both \' and "'`,
		"back\\slash": `'back\\slash'`, "a\nb\rc\td": `'a\nb\rc\td'`, "\x01\x7f": `'\x01\x7f'`, "é": `'é'`,
	} {
		if got := pythonRepr(in); got != want {
			t.Errorf("%q: %s, want %s", in, got, want)
		}
	}
	for in, want := range map[string]string{`null`: "None", `true`: "True", `false`: "False", `1.5`: "1.5", `[1]`: "[1]"} {
		var v any
		dec := json.NewDecoder(strings.NewReader(in))
		dec.UseNumber()
		_ = dec.Decode(&v)
		if got := pythonRepr(v); got != want {
			t.Errorf("%s: %s, want %s", in, got, want)
		}
	}
}
