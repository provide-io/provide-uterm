//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// echoRecord captures what the upstream WS observed, for identity/colormode
// assertions.
type echoRecord struct {
	gotControl chan map[string]any
	queries    chan string
}

// startEchoWS starts a fake upstream WS terminal server that echoes every
// terminal-data chunk back and records the first control frame + dial query.
func startEchoWS(t *testing.T) (string, *echoRecord) {
	t.Helper()
	rec := &echoRecord{gotControl: make(chan map[string]any, 8), queries: make(chan string, 8)}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow() //nolint:errcheck
		conn.SetReadLimit(-1)
		select {
		case rec.queries <- r.URL.RawQuery:
		default:
		}
		ctx := r.Context()
		dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
		for {
			typ, msg, rerr := conn.Read(ctx)
			if rerr != nil {
				return
			}
			if typ != websocket.MessageText {
				continue
			}
			events, derr := dec.Feed(string(msg))
			if derr != nil {
				continue
			}
			for _, ev := range events {
				switch e := ev.(type) {
				case controlchannel.ControlChunk:
					select {
					case rec.gotControl <- e.Control:
					default:
					}
				case controlchannel.DataChunk:
					out := controlchannel.EncodeTerminalData(e.Data)
					if conn.Write(ctx, websocket.MessageText, []byte(out)) != nil {
						return
					}
				}
			}
		}
	}))
	t.Cleanup(srv.Close)
	return "ws://" + strings.TrimPrefix(srv.URL, "http://") + "/ws", rec
}

func TestTelnetGatewayEcho(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &TelnetWsGateway{WSURL: wsURL, ColorMode: colors.ModePassthrough, IacNegotiate: false}
	ln, err := gw.Start("127.0.0.1", 0)
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close() //nolint:errcheck

	if _, err := conn.Write([]byte("ping")); err != nil {
		t.Fatalf("write: %v", err)
	}
	got := readWithTimeout(t, conn, 4)
	if string(got) != "ping" {
		t.Fatalf("echo = %q, want %q", got, "ping")
	}
}

func TestTelnetGatewayColorDowngrade(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &TelnetWsGateway{WSURL: wsURL, ColorMode: colors.Mode16, IacNegotiate: false}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close() //nolint:errcheck

	// A truecolor SGR should be downgraded to a 16-color SGR on the way back.
	seq := "\x1b[38;2;255;0;0mX\x1b[0m"
	_, _ = conn.Write([]byte(seq))
	got := readWithTimeout(t, conn, 12)
	if strings.Contains(string(got), "38;2;255") {
		t.Errorf("color not downgraded: %q", got)
	}
	if !strings.Contains(string(got), "X") {
		t.Errorf("payload lost: %q", got)
	}
}

func TestTelnetGatewayIacNegotiation(t *testing.T) {
	wsURL, rec := startEchoWS(t)
	gw := &TelnetWsGateway{
		WSURL: wsURL, ColorMode: colors.ModePassthrough,
		IacNegotiate: true, IacNegotiateTimeout: 2 * time.Second,
	}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close() //nolint:errcheck

	// Read the gateway's DO options, then reply WILL TTYPE and SB TTYPE IS.
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	hdr := make([]byte, 64)
	_, _ = conn.Read(hdr)
	_, _ = conn.Write([]byte{iacIAC, iacWILL, optTTYPE})
	ttype := append([]byte{iacIAC, iacSB, optTTYPE, subIS}, []byte("xterm-256color")...)
	ttype = append(ttype, iacIAC, iacSE)
	_, _ = conn.Write(ttype)
	_ = conn.SetReadDeadline(time.Time{})

	// The upstream dial should carry ?colormode=256 derived from the TTYPE.
	select {
	case q := <-rec.queries:
		if !strings.Contains(q, "colormode=256") {
			t.Fatalf("upstream query = %q, want colormode=256", q)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no upstream connection observed")
	}

	// And the session still pumps data end-to-end (the read may be preceded by
	// the late IAC SB TTYPE SEND negotiation reply, so scan for the payload).
	_, _ = conn.Write([]byte("go"))
	if got := readWithTimeout(t, conn, 8); !strings.Contains(string(got), "go") {
		t.Fatalf("echo = %q, want to contain %q", got, "go")
	}
}

func TestTelnetGatewaySecurityGate(t *testing.T) {
	gw := &TelnetWsGateway{WSURL: "ws://x/ws"}
	if _, err := gw.Start("0.0.0.0", 0); err == nil {
		t.Fatal("non-loopback bind without AllowUnauthenticated must fail")
	}
	gw.AllowUnauthenticated = true
	ln, err := gw.Start("0.0.0.0", 0)
	if err != nil {
		t.Fatalf("allow-unauthenticated bind should succeed: %v", err)
	}
	_ = ln.Close()
}

// readWithTimeout reads up to want bytes (or until 2s elapse) and returns them.
func readWithTimeout(t *testing.T, conn net.Conn, want int) []byte {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, 0, want)
	tmp := make([]byte, 256)
	for len(buf) < want {
		n, err := conn.Read(tmp)
		buf = append(buf, tmp[:n]...)
		if err != nil {
			break
		}
	}
	_ = conn.SetReadDeadline(time.Time{})
	return buf
}
