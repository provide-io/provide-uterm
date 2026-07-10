//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import "testing"

func TestSessionResultToMap(t *testing.T) {
	delta := "hello"
	ok := SessionResult{WorkerID: "w1", OK: true, OutputDelta: &delta, ElapsedMS: 12, Divergent: true}.toMap()
	if ok["worker_id"] != "w1" || ok["ok"] != true || ok["output_delta"] != "hello" ||
		ok["elapsed_ms"] != 12 || ok["divergent"] != true {
		t.Fatalf("ok toMap = %+v", ok)
	}

	// Failed session → output_delta must be nil (Python None).
	failed := SessionResult{WorkerID: "w2", OK: false}.toMap()
	if failed["output_delta"] != nil {
		t.Fatalf("failed output_delta = %v, want nil", failed["output_delta"])
	}
}

func TestResultMaps(t *testing.T) {
	d := "x"
	r := Result{Results: []SessionResult{
		{WorkerID: "w1", OK: true, OutputDelta: &d},
		{WorkerID: "w2", OK: false},
	}}
	maps := r.ResultMaps()
	if len(maps) != 2 || maps[0]["worker_id"] != "w1" || maps[1]["output_delta"] != nil {
		t.Fatalf("ResultMaps = %+v", maps)
	}
}
