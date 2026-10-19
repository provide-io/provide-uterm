//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

const subjectRevokePath = "/api/sessions/bb/viewers/revoke"

func revokeBody(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("body %q: %v", raw, err)
	}
	return body
}

func TestRevokingOneSubjectClosesOnlyItsSocketAndLetsAFreshTokenRejoin(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	url := base + "/ws/browser/bb/term"
	aliceHeaders := headers("share:bb:blackbetty:7", "viewer", 100)
	alice := dialWatcher(t, ctx, url, aliceHeaders)
	bob := dialWatcher(t, ctx, url, headers("share:bb:blackbetty:8", "viewer", 100))
	defer func() {
		for _, w := range []*closeWatcher{alice, bob} {
			_ = w.conn.Close(websocket.StatusNormalClosure, "")
		}
	}()
	alice.waitFrame(t, "hello")
	bob.waitFrame(t, "hello")

	rec := ts.do("POST", subjectRevokePath, `{"subject":"share:bb:blackbetty:7"}`, adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("revoke: %d %s", rec.Code, rec.Body.String())
	}
	body := revokeBody(t, rec.Body.Bytes())
	revokedAt, ok := body["revoked_at"].(float64)
	if !ok || len(body) != 6 || body["session_id"] != "bb" || body["closed"] != 1.0 ||
		body["close_code"] != 4410.0 || body["close_reason"] != "viewer access revoked" ||
		body["subject"] != "share:bb:blackbetty:7" {
		t.Fatalf("body %v", body)
	}
	if ce := alice.waitClose(t); ce.Code != 4410 || ce.Reason != "viewer access revoked" {
		t.Fatalf("alice closed with %d %q", ce.Code, ce.Reason)
	}
	payload, _ := controlchannel.EncodeControlFrame(map[string]any{"type": "ping"})
	if err := bob.conn.Write(ctx, websocket.MessageText, []byte(payload)); err != nil {
		t.Fatalf("write: %v", err)
	}
	bob.waitFrame(t, "pong")

	if rec := ts.do("GET", "/api/sessions/bb", "", flat(aliceHeaders)); rec.Code != http.StatusForbidden {
		t.Fatalf("revoked subject read: %d", rec.Code)
	}
	if rec := ts.do("GET", "/api/sessions/bb", "", flat(headers("share:bb:blackbetty:8", "viewer", 100))); rec.Code != http.StatusOK {
		t.Fatalf("other subject read: %d", rec.Code)
	}
	fresh := dialWatcher(t, ctx, url, headers("share:bb:blackbetty:7", "viewer", revokedAt+1))
	defer func() { _ = fresh.conn.Close(websocket.StatusNormalClosure, "") }()
	fresh.waitFrame(t, "hello")
}

func TestNoBodyAnEmptyOneOrNoSubjectStillRevokesTheWholeSession(t *testing.T) {
	for _, raw := range []string{"", " \t\r\n", "{}", "null", " null\n"} {
		ts := newTestServer(t, nil)
		ts.reg.add("bb", "owner1", "public")
		rec := ts.do("POST", subjectRevokePath, raw, adminHeaders())
		if rec.Code != http.StatusOK {
			t.Fatalf("%q: %d %s", raw, rec.Code, rec.Body.String())
		}
		body := revokeBody(t, rec.Body.Bytes())
		cutoff, ok := ts.srv.deps.Authz.ShareViewerRevocation("bb")
		if !ok || cutoff != body["revoked_at"] {
			t.Errorf("%q: session cutoff %v, %v", raw, cutoff, ok)
		}
		// A whole-session revoke keeps the original answer: no subject key.
		if _, echoed := body["subject"]; echoed || len(body) != 5 {
			t.Errorf("%q: answer %v", raw, body)
		}
	}
}

func TestAMalformedRevokeBodyIsA422ThatRevokesNothing(t *testing.T) {
	for _, raw := range []string{
		"not json", `{"subject": `, "[]", strings.Repeat("[", 2000) + strings.Repeat("]", 2000), `"share:bb:x"`, `{"subject": null}`, `{"subject": 7}`,
		`{"subject": "share:bb:x", "extra": 1}`, `{"subjects": "share:bb:x"}`, "\xff", `{"subject": "share:bb:` + "\xff" + `"}`,
		"\f", "\u00a0", `{} {}`, "X", "\ufeff{}", "\xff\xfe{\x00}\x00",
	} {
		ts := newTestServer(t, nil)
		ts.reg.add("bb", "owner1", "public")
		rec := ts.do("POST", subjectRevokePath, raw, adminHeaders())
		if rec.Code != http.StatusUnprocessableEntity || rec.Body.String() != `{"detail":"invalid request body"}`+"\n" {
			t.Errorf("%q: %d %q", raw, rec.Code, rec.Body.String())
		}
		if _, ok := ts.srv.deps.Authz.ShareViewerRevocation("bb"); ok {
			t.Errorf("%q: a session cutoff was recorded", raw)
		}
	}
}

func TestASubjectThatIsNotThisSessionsShareSubjectIsA422(t *testing.T) {
	for _, subject := range []string{"share:other:7", "share:bbx:7", "share:bb", "share:bb:", "alice"} {
		ts := newTestServer(t, nil)
		ts.reg.add("bb", "owner1", "public")
		raw, _ := json.Marshal(map[string]string{"subject": subject})
		rec := ts.do("POST", subjectRevokePath, string(raw), adminHeaders())
		if rec.Code != http.StatusUnprocessableEntity ||
			rec.Body.String() != `{"detail":"subject must be a share subject of this session"}`+"\n" {
			t.Errorf("%q: %d %q", subject, rec.Code, rec.Body.String())
		}
		if _, ok := ts.srv.deps.Authz.ShareViewerSubjectRevocation("bb", subject); ok {
			t.Errorf("%q: recorded", subject)
		}
	}
}

func TestASubjectSpellingALoneSurrogateIsMalformed(t *testing.T) {
	cases := []struct {
		escape string
		code   int
	}{
		{`\ud83d\ude00`, http.StatusOK},
		{`\\ud800`, http.StatusOK},
		{`\u0041`, http.StatusOK},
		{`\ud800`, http.StatusUnprocessableEntity},
		{`\udc00`, http.StatusUnprocessableEntity},
		{`\ud800x`, http.StatusUnprocessableEntity},
		{`\ud800\u0041`, http.StatusUnprocessableEntity},
		{`\ud800\ud800`, http.StatusUnprocessableEntity},
		{`\ude00\ud83d`, http.StatusUnprocessableEntity},
	}
	for _, c := range cases {
		ts := newTestServer(t, nil)
		ts.reg.add("bb", "owner1", "public")
		rec := ts.do("POST", subjectRevokePath, `{"subject": "share:bb:`+c.escape+`"}`, adminHeaders())
		if rec.Code != c.code {
			t.Errorf("%s: %d %s", c.escape, rec.Code, rec.Body.String())
		}
	}
}

func TestASessionFullOfRevokedSubjectsAnswers429(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	now := ts.srv.deps.Clock.Wall()
	for n := 0; n < serverauth.ShareViewerSubjectRevocationsPerSessionMax; n++ {
		_, _ = ts.srv.deps.Authz.RevokeShareViewerSubject("bb", fmt.Sprintf("share:bb:%d", n), now)
	}
	rec := ts.do("POST", subjectRevokePath, `{"subject":"share:bb:blackbetty:7"}`, adminHeaders())
	want := `{"detail":"too many viewers of this session revoked one by one; revoke them all instead"}` + "\n"
	if rec.Code != http.StatusTooManyRequests || rec.Body.String() != want {
		t.Fatalf("full session: %d %q", rec.Code, rec.Body.String())
	}
	if rec := ts.do("POST", subjectRevokePath, "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("whole-session revoke: %d", rec.Code)
	}
}

func TestARefusedCallerIsRefusedBeforeItsBodyIsJudged(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	if rec := ts.do("POST", subjectRevokePath, "not json", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("refused caller: %d", rec.Code)
	}
}

func TestASubjectRevokeIsAuditedWithItsSubject(t *testing.T) {
	var logs bytes.Buffer
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Logger = slog.New(slog.NewTextHandler(&logs, nil))
	})
	ts.reg.add("bb", "owner1", "public")
	if rec := ts.do("POST", subjectRevokePath, `{"subject":"share:bb:blackbetty:7"}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("revoke: %d", rec.Code)
	}
	if rec := ts.do("POST", subjectRevokePath, "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("whole-session revoke: %d", rec.Code)
	}
	var revokes []string
	for _, line := range strings.Split(logs.String(), "\n") {
		if strings.Contains(line, "action=session.viewers.revoke") {
			revokes = append(revokes, line)
		}
	}
	if len(revokes) != 2 || !strings.Contains(revokes[0], "subject:share:bb:blackbetty:7") || strings.Contains(revokes[1], "subject:") {
		t.Fatalf("revoke audits %q", revokes)
	}
}

func TestDeletingASessionForgetsItsSubjectCutoffs(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	if rec := ts.do("POST", subjectRevokePath, `{"subject":"share:bb:blackbetty:7"}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("revoke %d", rec.Code)
	}
	if rec := ts.do("DELETE", "/api/sessions/bb", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("delete %d", rec.Code)
	}
	if _, ok := ts.srv.deps.Authz.ShareViewerSubjectRevocation("bb", "share:bb:blackbetty:7"); ok {
		t.Fatal("subject cutoff kept after delete")
	}
}

func TestARevokeBodyPastTheCapIsA422AndOneAtItIsRead(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	over := strings.Repeat(" ", serverauth.ShareViewerRevokeBodyMaxBytes-1) + "{}"
	if rec := ts.do("POST", subjectRevokePath, over, adminHeaders()); rec.Code != http.StatusUnprocessableEntity ||
		rec.Body.String() != `{"detail":"invalid request body"}`+"\n" {
		t.Fatalf("over the cap: %d %q", rec.Code, rec.Body.String())
	}
	at := strings.Repeat(" ", serverauth.ShareViewerRevokeBodyMaxBytes-2) + "{}"
	if rec := ts.do("POST", subjectRevokePath, at, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("at the cap: %d %s", rec.Code, rec.Body.String())
	}
	if serverauth.ShareViewerRevokeBodyMaxBytes != 4096 {
		t.Error("the body cap changed")
	}
}

func TestAFullSubjectTableAnswers429(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	now := ts.srv.deps.Clock.Wall()
	per := serverauth.ShareViewerSubjectRevocationsPerSessionMax
	for n := 0; n < serverauth.ShareViewerSubjectRevocationsMax; n++ {
		_, _ = ts.srv.deps.Authz.RevokeShareViewerSubject(fmt.Sprintf("s%d", n/per), fmt.Sprintf("share:s%d:%d", n/per, n%per), now)
	}
	rec := ts.do("POST", subjectRevokePath, `{"subject":"share:bb:blackbetty:7"}`, adminHeaders())
	want := `{"detail":"too many viewers revoked one by one; try again later"}` + "\n"
	if rec.Code != http.StatusTooManyRequests || rec.Body.String() != want {
		t.Fatalf("full table: %d %q", rec.Code, rec.Body.String())
	}
}

func TestAWholeSessionRevokeFreesTheSessionsSubjectSlots(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	now := ts.srv.deps.Clock.Wall()
	for n := 0; n < serverauth.ShareViewerSubjectRevocationsPerSessionMax; n++ {
		_, _ = ts.srv.deps.Authz.RevokeShareViewerSubject("bb", fmt.Sprintf("share:bb:%d", n), now)
	}
	if rec := ts.do("POST", subjectRevokePath, "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("whole-session revoke: %d", rec.Code)
	}
	if rec := ts.do("POST", subjectRevokePath, `{"subject":"share:bb:blackbetty:7"}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("subject revoke after freeing: %d %s", rec.Code, rec.Body.String())
	}
}
