//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"fmt"
	"sync"
	"testing"
)

// TestPresenceStoreConcurrent hammers a single store from many goroutines
// (add/update/get/owner/prune/sync). It must stay data-race-free under
// `go test -race`; the assertions are about not crashing, not exact state.
func TestPresenceStoreConcurrent(t *testing.T) {
	s := NewPresenceStore()
	for i := 0; i < 50; i++ {
		s.Add(fmt.Sprintf("u%d", i), "n", colors[i%len(colors)], "viewer", "")
	}
	var wg sync.WaitGroup
	for g := 0; g < 16; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 500; i++ {
				uid := fmt.Sprintf("u%d", i%50)
				switch i % 6 {
				case 0:
					_, _, _ = s.Update(uid, map[string]any{"scroll_line": i, "typing": i%2 == 0})
				case 1:
					s.Get(uid)
				case 2:
					s.SetOwner(uid)
				case 3:
					s.GetOwner()
				case 4:
					s.GetSyncPayload(map[string]any{"k": 1})
				case 5:
					s.TakenColors()
					s.GetAll()
					s.Count()
				}
			}
		}(g)
	}
	wg.Wait()
}

// TestServiceConcurrent drives the full service from many goroutines across
// several workers and browser connections concurrently.
func TestServiceConcurrent(t *testing.T) {
	d := NewDeckMuxPresence(&fakeHub{})
	var wg sync.WaitGroup
	for g := 0; g < 12; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			worker := fmt.Sprintf("w%d", g%3)
			ws := &fakeWS{}
			_, _ = d.OnBrowserConnect(worker, ws, "operator", nil)
			for i := 0; i < 200; i++ {
				switch i % 4 {
				case 0:
					_ = d.HandleMessage(worker, ws, map[string]any{"type": "presence_update", "scroll_line": i}, nil)
				case 1:
					_ = d.HandleMessage(worker, ws, map[string]any{"type": "queued_input", "keys": "ls\r"}, nil)
				case 2:
					_ = d.HandleMessage(worker, ws, map[string]any{"type": "control_request"}, nil)
				case 3:
					d.GetTransferManager(worker, nil).CheckAutoTransfer(float64(i), []string{"x"})
				}
			}
			_ = d.OnBrowserDisconnect(worker, ws, nil)
		}(g)
	}
	wg.Wait()
}

// TestTransferManagerConcurrent hammers one manager's queue + warning state.
func TestTransferManagerConcurrent(t *testing.T) {
	tm := NewTransferManager(30, "display")
	var wg sync.WaitGroup
	for g := 0; g < 16; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			uid := fmt.Sprintf("u%d", g%4)
			for i := 0; i < 500; i++ {
				switch i % 4 {
				case 0:
					tm.QueueKeystroke(uid, "a")
				case 1:
					tm.GetQueueDisplay(uid)
				case 2:
					tm.CheckAutoTransfer(float64(i%40), []string{"x"})
				case 3:
					tm.BuildTransferMessage("a", uid, "handover")
				}
			}
		}(g)
	}
	wg.Wait()
}
