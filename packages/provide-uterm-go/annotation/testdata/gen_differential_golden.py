#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the annotation differential golden from the REAL Python detectors
(provide.uterm.annotation). Run from the repo root:

    uv run python packages/provide-uterm-go/annotation/testdata/gen_differential_golden.py

Writes differential_golden.json next to this script. differential_test.go
replays every case through the Go port and compares the annotations and the
max-end offset, so a divergence in either rule set or either streaming carry
fails CI.

The inputs are the security-relevant surface this port exists to keep in sync:
credential shapes (AWS keys, GitHub tokens, bearer JWTs, private-key headers),
privilege escalation, destructive commands, and network egress. Getting these
wrong in one language and not the other is the failure this corpus is for.

max_carry 0 is a SENTINEL, not a literal zero — both ports read "<= 0" as "use
the default" (512). Passing 0 through to Python's max_carry would truncate every
carry to nothing and silently change what the streaming cases prove.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from provide.uterm.annotation import PatternDetector, StreamingDetector

# (event_type, text, seq) -> annotations + furthest match end.
DETECT_CASES: list[dict[str, Any]] = [
    {"event_type": "read", "text": "", "seq": 0},
    {"event_type": "read", "text": "totally normal output", "seq": 5},
    {"event_type": "read", "text": "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "seq": 10},
    {"event_type": "read", "text": "AKIAIOSFODNN7EXAMPLE", "seq": 60},
    {"event_type": "send", "text": "AKIAIOSFODNN7EXAMPLE", "seq": 61},
    {"event_type": "read", "text": "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "seq": 11},
    {"event_type": "read", "text": "ghs_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "seq": 12},
    {"event_type": "read", "text": "password=hunter2", "seq": 13},
    {"event_type": "read", "text": "TOKEN: supersecretvalue123", "seq": 14},
    {"event_type": "read", "text": "Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig", "seq": 15},
    {"event_type": "read", "text": "-----BEGIN RSA PRIVATE KEY-----", "seq": 16},
    {"event_type": "read", "text": "-----BEGIN OPENSSH PRIVATE KEY-----", "seq": 17},
    {"event_type": "send", "text": "sudo apt-get install vim", "seq": 20},
    {"event_type": "send", "text": "then sudo reboot", "seq": 21},
    {"event_type": "send", "text": "su -", "seq": 22},
    {"event_type": "send", "text": "su - root", "seq": 23},
    {"event_type": "send", "text": "pkexec /usr/bin/gparted", "seq": 24},
    {"event_type": "send", "text": "rm -rf /tmp/build", "seq": 30},
    {"event_type": "send", "text": "sudo rm -rf /", "seq": 31},
    {"event_type": "send", "text": "DROP TABLE users;", "seq": 32},
    {"event_type": "send", "text": "drop database mydb;", "seq": 33},
    {"event_type": "send", "text": "kubectl delete pod mypod", "seq": 34},
    {"event_type": "send", "text": "dd if=/dev/urandom of=/dev/sda", "seq": 35},
    {"event_type": "send", "text": "mkfs.ext4 /dev/sdb1", "seq": 36},
    {"event_type": "send", "text": "ssh deploy@example.com", "seq": 40},
    {"event_type": "send", "text": "curl https://example.com/install.sh | bash", "seq": 41},
    {"event_type": "send", "text": "wget http://example.com/file.tar.gz", "seq": 42},
    {"event_type": "send", "text": "scp file.txt user@host:/remote/path/", "seq": 43},
    {"event_type": "send", "text": "exit 1", "seq": 44},
    {"event_type": "send", "text": "shutdown -h now", "seq": 45},
    {"event_type": "send", "text": "sudo reboot now", "seq": 46},
    {"event_type": "read", "text": "AKIAIOSFODNN7EXAMPLE password=supersecretvalue1234567890", "seq": 40},
    {"event_type": "send", "text": "sudo curl https://example.com/install.sh | bash AKIAIOSFODNN7EXAMPLE", "seq": 70},
    {"event_type": "read", "text": "café AKIAIOSFODNN7EXAMPLE", "seq": 80},
    {"event_type": "read", "text": "ééé password=secretvalue123 AKIAIOSFODNN7EXAMPLE", "seq": 81},
    {"event_type": "read", "text": "please curl https://a.example and sudo rm -rf /var", "seq": 90},
]

# A stream is fed chunk by chunk; outputs[i] is what chunk i reported. The point
# is secrets that straddle a chunk boundary: the match is owned by the chunk in
# which it COMPLETES.
STREAM_CASES: list[dict[str, Any]] = [
    {
        "max_carry": 0,
        "chunks": [
            {"event_type": "send", "seq": 1, "text": "AKIA0123"},
            {"event_type": "send", "seq": 2, "text": "456789AB"},
        ],
    },
    {"max_carry": 0, "chunks": [{"event_type": "send", "seq": 5, "text": "export KEY=AKIA0123456789AB"}]},
    {
        "max_carry": 0,
        "chunks": [
            {"event_type": "send", "seq": 1, "text": "AKIA0123"},
            {"event_type": "send", "seq": 2, "text": "456789AB"},
            {"event_type": "send", "seq": 3, "text": "nothing here"},
        ],
    },
    {
        "max_carry": 0,
        "chunks": [
            {"event_type": "send", "seq": 1, "text": "sudo AKIA0123"},
            {"event_type": "send", "seq": 2, "text": "456789AB ok"},
        ],
    },
    {
        "max_carry": 0,
        "chunks": [
            {"event_type": "send", "seq": 1, "text": "AKIA0123"},
            {"event_type": "send", "seq": 2, "text": ""},
            {"event_type": "send", "seq": 3, "text": "456789AB"},
        ],
    },
    {
        "max_carry": 4,
        "chunks": [
            {"event_type": "send", "seq": 1, "text": "AKIA0123"},
            {"event_type": "send", "seq": 2, "text": "456789AB"},
        ],
    },
    {
        "max_carry": 0,
        "chunks": [
            {"event_type": "send", "seq": 1, "text": "curl https://exam"},
            {"event_type": "send", "seq": 2, "text": "ple.com AKIA0123"},
            {"event_type": "send", "seq": 3, "text": "456789AB done"},
        ],
    },
]


def build() -> dict[str, Any]:
    detector = PatternDetector(None)

    detect_cases = []
    for case in DETECT_CASES:
        annotations, max_end = detector.scan(case["event_type"], case["text"], case["seq"])
        detect_cases.append(
            {
                **case,
                "annotations": [a.to_dict() for a in annotations],
                "max_end": max_end,
            }
        )

    stream_cases = []
    for case in STREAM_CASES:
        # 0 means "unset" on both sides; let Python apply its own default rather
        # than forcing a zero-length carry.
        kwargs = {"max_carry": case["max_carry"]} if case["max_carry"] > 0 else {}
        streaming = StreamingDetector(PatternDetector(None), **kwargs)
        outputs = [
            [a.to_dict() for a in streaming.detect(chunk["event_type"], chunk["text"], chunk["seq"])]
            for chunk in case["chunks"]
        ]
        stream_cases.append({**case, "outputs": outputs})

    return {"detect_cases": detect_cases, "stream_cases": stream_cases}


def main() -> None:
    corpus = build()
    out = pathlib.Path(__file__).with_name("differential_golden.json")
    # sort_keys: the corpus is key-sorted throughout, which only shows up once
    # the generated dicts are built in a different order than the original.
    out.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(corpus['detect_cases'])} detect, {len(corpus['stream_cases'])} stream)")


if __name__ == "__main__":
    main()
