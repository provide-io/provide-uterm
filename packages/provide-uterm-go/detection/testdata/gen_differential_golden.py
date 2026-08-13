#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the detection differential golden from the REAL Python detection
stack (provide.uterm.detection). Run from the repo root:

    uv run python packages/provide-uterm-go/detection/testdata/gen_differential_golden.py

Writes differential_golden.json next to this script. differential_test.go
replays all four families through the Go port, so a divergence in prompt
matching, flow advancement, idle detection or input-type inference fails CI.

The idle cases pin the clock. BufferManager reads time.time() both when it
stamps a screen without a captured_at and when it measures staleness, so a
generator that let the real clock run would produce a different corpus every
second. Go patches its own nowSeconds for the same reason; this patches
detection.buffer.time.time to the per-case "now".
"""

from __future__ import annotations

import json
import pathlib
from typing import Any
from unittest.mock import patch

from provide.uterm.detection.buffer import BufferManager
from provide.uterm.detection.detector import PromptDetector
from provide.uterm.detection.extractor import extract_kv
from provide.uterm.detection.flow import FlowEngine
from provide.uterm.detection.input_type import auto_detect_input_type
from provide.uterm.detection.loader import load_ruleset

DETECTOR_CASES: list[dict[str, Any]] = [
    {
        "name": "region_match",
        "patterns": [
            {"id": "prompt.login", "input_type": "multi_key", "regex": "Enter your name:"},
            {"id": "prompt.password", "input_type": "multi_key", "regex": "Password:"},
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 1},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Welcome\nEnter your name:",
            "screen_hash": "cfa1bcc8a9bce6caac97885bd99d47db85be88d410edb060aa797958230f2bac",
        },
    },
    {
        "name": "no_match",
        "patterns": [
            {"id": "prompt.login", "input_type": "multi_key", "regex": "Enter your name:"},
            {"id": "prompt.password", "input_type": "multi_key", "regex": "Password:"},
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Just some text",
            "screen_hash": "7dfcb0a570f9fe1ae7300fa2e47829fd453219a93664daa759976d71c26b01ab",
        },
    },
    {
        "name": "password_match",
        "patterns": [
            {"id": "prompt.login", "input_type": "multi_key", "regex": "Enter your name:"},
            {"id": "prompt.password", "input_type": "multi_key", "regex": "Password:"},
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Password:",
            "screen_hash": "569b2482a687d9aa4dd67be6a8e4c171621dd01ac251455cfe56b2cc8d1b25d6",
        },
    },
    {
        "name": "negative_excludes",
        "patterns": [
            {
                "id": "prompt.buy",
                "input_type": "single_key",
                "negative_match": {"match_mode": "regex", "pattern": "stardock"},
                "regex": "which item",
            }
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 1},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "stardock\nwhich item",
            "screen_hash": "27f24636a85a1df6bb8d3814efc687155ca53ab337becefdf40bcf10dff8de4a",
        },
    },
    {
        "name": "negative_allows",
        "patterns": [
            {
                "id": "prompt.buy",
                "input_type": "single_key",
                "negative_match": {"match_mode": "regex", "pattern": "stardock"},
                "regex": "which item",
            }
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 1},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "shop\nwhich item",
            "screen_hash": "bf31658ddb0298ed61d71f2b372af1b1216754cca1a92e1cc0e8f79c3a65111f",
        },
    },
    {
        "name": "negative_absent",
        "patterns": [
            {
                "id": "prompt.buy",
                "input_type": "single_key",
                "negative_match": {"match_mode": "regex", "pattern": "stardock"},
                "regex": "which item",
            }
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 1},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "regular store\nwhich item",
            "screen_hash": "3db7a9776d45f7a3de465f1876c22f0890e9bb2736e3c4e719f9d74c5986f631",
        },
    },
    {
        "name": "negative_regex_ci",
        "patterns": [{"id": "p", "input_type": "single_key", "negative_regex": "STARDOCK", "regex": "which item"}],
        "snapshot": {
            "cursor": {"x": 0, "y": 1},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "stardock here\nwhich item",
            "screen_hash": "0471d642a411a5883ee56acaad2ca3e5d261083b05a08303ffe79158cf2f8c69",
        },
    },
    {
        "name": "order_first_wins",
        "patterns": [
            {"id": "p.first", "input_type": "single_key", "regex": "Hello"},
            {"id": "p.second", "input_type": "multi_key", "regex": "Hello there"},
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Hello there",
            "screen_hash": "4e47826698bb4630fb4451010062fadbf85d61427cbdfaed7ad0f23f239bed89",
        },
    },
    {
        "name": "input_type_single_key",
        "patterns": [{"id": "p", "input_type": "single_key", "regex": "go:"}],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "ready go:",
            "screen_hash": "2c1557fee4ba46c9f2fedc7f26cdab52801245921bc58364d4b4e27706fb0fd8",
        },
    },
    {
        "name": "input_type_multi_key",
        "patterns": [{"id": "p", "input_type": "multi_key", "regex": "go:"}],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "ready go:",
            "screen_hash": "2c1557fee4ba46c9f2fedc7f26cdab52801245921bc58364d4b4e27706fb0fd8",
        },
    },
    {
        "name": "input_type_any_key",
        "patterns": [{"id": "p", "input_type": "any_key", "regex": "go:"}],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "ready go:",
            "screen_hash": "2c1557fee4ba46c9f2fedc7f26cdab52801245921bc58364d4b4e27706fb0fd8",
        },
    },
    {
        "name": "input_type_menu_choice",
        "patterns": [{"id": "p", "input_type": "menu_choice", "regex": "go:"}],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "ready go:",
            "screen_hash": "2c1557fee4ba46c9f2fedc7f26cdab52801245921bc58364d4b4e27706fb0fd8",
        },
    },
    {
        "name": "input_type_none",
        "patterns": [{"id": "p", "input_type": "none", "regex": "go:"}],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "ready go:",
            "screen_hash": "2c1557fee4ba46c9f2fedc7f26cdab52801245921bc58364d4b4e27706fb0fd8",
        },
    },
    {
        "name": "cursor_miss_no_trailing",
        "patterns": [
            {"expect_cursor_at_end": True, "id": "prompt.login", "input_type": "multi_key", "regex": "Enter your name:"}
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": False,
            "has_trailing_space": False,
            "screen": "Enter your name:\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline",
            "screen_hash": "a387f2af5671b27955993980f108226f0b16b5a8f6620242eda46a0e76af8416",
        },
    },
    {
        "name": "cursor_miss_with_trailing",
        "patterns": [
            {"expect_cursor_at_end": True, "id": "prompt.login", "input_type": "multi_key", "regex": "Enter your name:"}
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": False,
            "has_trailing_space": True,
            "screen": "Enter your name:\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline",
            "screen_hash": "a387f2af5671b27955993980f108226f0b16b5a8f6620242eda46a0e76af8416",
        },
    },
    {
        "name": "full_screen_fallback",
        "patterns": [
            {"expect_cursor_at_end": True, "id": "prompt.login", "input_type": "multi_key", "regex": "Enter your name:"}
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Enter your name:\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline\nline",
            "screen_hash": "a387f2af5671b27955993980f108226f0b16b5a8f6620242eda46a0e76af8416",
        },
    },
    {
        "name": "no_cursor_required",
        "patterns": [{"expect_cursor_at_end": False, "id": "p", "input_type": "any_key", "regex": "more"}],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": False,
            "has_trailing_space": False,
            "screen": "press any key for more",
            "screen_hash": "7c174b053a4af5d79b311f20d96d656346ceb581b613c966c4939c10949557ee",
        },
    },
    {
        "name": "kv_extract",
        "patterns": [
            {
                "id": "prompt.sector",
                "input_type": "single_key",
                "kv_extract": [
                    {"field": "sector", "regex": "Sector\\s+(\\d+)", "type": "int"},
                    {"field": "credits", "regex": "Credits:\\s+([\\d,]+)", "type": "int"},
                ],
                "regex": "Sector\\s+\\d+\\s*:",
            }
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 1},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Sector 42 : Credits: 15,000\nCommand prompt",
            "screen_hash": "f476170b43b3996fc303f64c5404ebedb8cd391fdc11537c3334a1736f453487",
        },
    },
    {
        "name": "kv_extract_partial",
        "patterns": [
            {
                "id": "prompt.sector",
                "input_type": "single_key",
                "kv_extract": [
                    {"field": "sector", "regex": "Sector\\s+(\\d+)", "type": "int"},
                    {"field": "credits", "regex": "Credits:\\s+([\\d,]+)", "type": "int"},
                ],
                "regex": "Sector\\s+\\d+\\s*:",
            }
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Sector 99 :",
            "screen_hash": "a786a66439550e823d8ec3485b412b6317341cc967f1bc8616ec7dc82685afa7",
        },
    },
    {
        "name": "kv_validation_max_fail",
        "patterns": [
            {
                "id": "p",
                "input_type": "multi_key",
                "kv_extract": [
                    {"field": "score", "regex": "Score:\\s*(\\d+)", "type": "int", "validate": {"max": 100}}
                ],
                "regex": "Score",
            }
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Score: 5000",
            "screen_hash": "973901b21623b63fe97eba729e5823167676b5330176637e81819e8810f8aebc",
        },
    },
    {
        "name": "kv_types",
        "patterns": [
            {
                "id": "p",
                "input_type": "multi_key",
                "kv_extract": [
                    {"field": "temp", "regex": "Temp:\\s*([\\d.]+)", "type": "float"},
                    {"field": "ansi", "regex": "ANSI:\\s*(\\w+)", "type": "bool"},
                    {"field": "name", "regex": "Name:\\s*(\\w+)", "type": "string"},
                ],
                "regex": "Status",
            }
        ],
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Status Temp: 98.6 ANSI: Yes Name: Alice",
            "screen_hash": "899f70cfc4c6cd71f7c8a6afa76004306cec42ac3e6c1d84e1c196ce8fbf3436",
        },
    },
    {
        "name": "ruleset_contains",
        "rules": {
            "game": "t",
            "prompts": [
                {
                    "id": "prompt.login",
                    "input_type": "multi_key",
                    "match": {"match_mode": "contains", "pattern": "Enter your name"},
                }
            ],
            "version": "1.0",
        },
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Enter your name",
            "screen_hash": "0421de120444dad31b883893333d474e505557cf255c2afd5d86389247ab6094",
        },
    },
    {
        "name": "ruleset_exact",
        "rules": {
            "game": "t",
            "prompts": [
                {"id": "p", "input_type": "any_key", "match": {"match_mode": "exact", "pattern": "[Press ENTER]"}}
            ],
            "version": "1.0",
        },
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "[Press ENTER]",
            "screen_hash": "1154420964470e5eb82e1da07bad5d074b9fe085b83220f9fc305e3a4b318612",
        },
    },
    {
        "name": "ruleset_regex_end_anchor",
        "rules": {
            "game": "t",
            "prompts": [
                {
                    "id": "cmd",
                    "input_type": "single_key",
                    "match": {"match_mode": "regex", "pattern": "Command \\[.*\\Z"},
                }
            ],
            "version": "1.0",
        },
        "snapshot": {
            "cursor": {"x": 0, "y": 0},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "Command [TL=00:00]:\n\n",
            "screen_hash": "c0545e238a9bbc2b221b18bbc4dc057fe5ed09ab21216a21935377e39f828ba1",
        },
    },
    {
        "name": "ruleset_negative_contains",
        "rules": {
            "game": "t",
            "prompts": [
                {
                    "id": "p",
                    "input_type": "single_key",
                    "match": {"match_mode": "contains", "pattern": "choose"},
                    "negative_match": {"match_mode": "contains", "pattern": "stardock"},
                }
            ],
            "version": "1.0",
        },
        "snapshot": {
            "cursor": {"x": 0, "y": 1},
            "cursor_at_end": True,
            "has_trailing_space": False,
            "screen": "welcome to stardock\nchoose",
            "screen_hash": "6c38e9975d3fcc5838b82a57008aaae8dda8b84a5cc6c10b85f89d619aaaaff7",
        },
    },
]

FLOW_CASES: list[dict[str, Any]] = [
    {
        "flow": "login",
        "name": "flow_name",
        "rules": {
            "flows": [
                {
                    "description": "login flow",
                    "id": "login",
                    "steps": [
                        {
                            "expects_prompt": "login.name",
                            "gate_prompts": ["login.name"],
                            "id": "send_name",
                            "keys": "alice\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "login.password",
                            "gate_prompts": ["login.password"],
                            "id": "send_password",
                            "keys": "secret\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "main.command",
                            "gate_prompts": ["main.command"],
                            "id": "done",
                            "kind": "noop",
                        },
                    ],
                }
            ],
            "game": "test",
            "prompts": [
                {
                    "id": "login.name",
                    "input_type": "multi_key",
                    "kv_extract": [{"field": "attempt", "regex": "Attempt\\s+(\\d+)", "type": "int"}],
                    "match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "login.password",
                    "input_type": "multi_key",
                    "match": {"match_mode": "contains", "pattern": "Enter password"},
                    "negative_match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "main.command",
                    "input_type": "single_key",
                    "match": {"match_mode": "contains", "pattern": "Command ["},
                },
            ],
            "version": "1.0",
        },
        "screen": "Attempt 3\r\nEnter your name:",
    },
    {
        "flow": "login",
        "name": "flow_negative",
        "rules": {
            "flows": [
                {
                    "description": "login flow",
                    "id": "login",
                    "steps": [
                        {
                            "expects_prompt": "login.name",
                            "gate_prompts": ["login.name"],
                            "id": "send_name",
                            "keys": "alice\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "login.password",
                            "gate_prompts": ["login.password"],
                            "id": "send_password",
                            "keys": "secret\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "main.command",
                            "gate_prompts": ["main.command"],
                            "id": "done",
                            "kind": "noop",
                        },
                    ],
                }
            ],
            "game": "test",
            "prompts": [
                {
                    "id": "login.name",
                    "input_type": "multi_key",
                    "kv_extract": [{"field": "attempt", "regex": "Attempt\\s+(\\d+)", "type": "int"}],
                    "match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "login.password",
                    "input_type": "multi_key",
                    "match": {"match_mode": "contains", "pattern": "Enter password"},
                    "negative_match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "main.command",
                    "input_type": "single_key",
                    "match": {"match_mode": "contains", "pattern": "Command ["},
                },
            ],
            "version": "1.0",
        },
        "screen": "Enter password:",
    },
    {
        "flow": "login",
        "name": "flow_terminal",
        "rules": {
            "flows": [
                {
                    "description": "login flow",
                    "id": "login",
                    "steps": [
                        {
                            "expects_prompt": "login.name",
                            "gate_prompts": ["login.name"],
                            "id": "send_name",
                            "keys": "alice\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "login.password",
                            "gate_prompts": ["login.password"],
                            "id": "send_password",
                            "keys": "secret\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "main.command",
                            "gate_prompts": ["main.command"],
                            "id": "done",
                            "kind": "noop",
                        },
                    ],
                }
            ],
            "game": "test",
            "prompts": [
                {
                    "id": "login.name",
                    "input_type": "multi_key",
                    "kv_extract": [{"field": "attempt", "regex": "Attempt\\s+(\\d+)", "type": "int"}],
                    "match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "login.password",
                    "input_type": "multi_key",
                    "match": {"match_mode": "contains", "pattern": "Enter password"},
                    "negative_match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "main.command",
                    "input_type": "single_key",
                    "match": {"match_mode": "contains", "pattern": "Command ["},
                },
            ],
            "version": "1.0",
        },
        "screen": "Command [TL=00:00]:",
    },
    {
        "flow": "login",
        "name": "flow_tail_over_scrollback",
        "rules": {
            "flows": [
                {
                    "description": "login flow",
                    "id": "login",
                    "steps": [
                        {
                            "expects_prompt": "login.name",
                            "gate_prompts": ["login.name"],
                            "id": "send_name",
                            "keys": "alice\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "login.password",
                            "gate_prompts": ["login.password"],
                            "id": "send_password",
                            "keys": "secret\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "main.command",
                            "gate_prompts": ["main.command"],
                            "id": "done",
                            "kind": "noop",
                        },
                    ],
                }
            ],
            "game": "test",
            "prompts": [
                {
                    "id": "login.name",
                    "input_type": "multi_key",
                    "kv_extract": [{"field": "attempt", "regex": "Attempt\\s+(\\d+)", "type": "int"}],
                    "match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "login.password",
                    "input_type": "multi_key",
                    "match": {"match_mode": "contains", "pattern": "Enter password"},
                    "negative_match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "main.command",
                    "input_type": "single_key",
                    "match": {"match_mode": "contains", "pattern": "Command ["},
                },
            ],
            "version": "1.0",
        },
        "screen": "Enter your name\r\nalice\r\nCommand [TL=00:00]:",
    },
    {
        "flow": "login",
        "name": "flow_earlier_step_tail",
        "rules": {
            "flows": [
                {
                    "description": "login flow",
                    "id": "login",
                    "steps": [
                        {
                            "expects_prompt": "login.name",
                            "gate_prompts": ["login.name"],
                            "id": "send_name",
                            "keys": "alice\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "login.password",
                            "gate_prompts": ["login.password"],
                            "id": "send_password",
                            "keys": "secret\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "main.command",
                            "gate_prompts": ["main.command"],
                            "id": "done",
                            "kind": "noop",
                        },
                    ],
                }
            ],
            "game": "test",
            "prompts": [
                {
                    "id": "login.name",
                    "input_type": "multi_key",
                    "kv_extract": [{"field": "attempt", "regex": "Attempt\\s+(\\d+)", "type": "int"}],
                    "match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "login.password",
                    "input_type": "multi_key",
                    "match": {"match_mode": "contains", "pattern": "Enter password"},
                    "negative_match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "main.command",
                    "input_type": "single_key",
                    "match": {"match_mode": "contains", "pattern": "Command ["},
                },
            ],
            "version": "1.0",
        },
        "screen": "Command [TL=00:00]:\r\nx\r\nEnter your name:",
    },
    {
        "flow": "login",
        "name": "flow_no_match",
        "rules": {
            "flows": [
                {
                    "description": "login flow",
                    "id": "login",
                    "steps": [
                        {
                            "expects_prompt": "login.name",
                            "gate_prompts": ["login.name"],
                            "id": "send_name",
                            "keys": "alice\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "login.password",
                            "gate_prompts": ["login.password"],
                            "id": "send_password",
                            "keys": "secret\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "main.command",
                            "gate_prompts": ["main.command"],
                            "id": "done",
                            "kind": "noop",
                        },
                    ],
                }
            ],
            "game": "test",
            "prompts": [
                {
                    "id": "login.name",
                    "input_type": "multi_key",
                    "kv_extract": [{"field": "attempt", "regex": "Attempt\\s+(\\d+)", "type": "int"}],
                    "match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "login.password",
                    "input_type": "multi_key",
                    "match": {"match_mode": "contains", "pattern": "Enter password"},
                    "negative_match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "main.command",
                    "input_type": "single_key",
                    "match": {"match_mode": "contains", "pattern": "Command ["},
                },
            ],
            "version": "1.0",
        },
        "screen": "No prompt here",
    },
    {
        "cursor": [7, 8],
        "flow": "login",
        "name": "flow_cursor",
        "rules": {
            "flows": [
                {
                    "description": "login flow",
                    "id": "login",
                    "steps": [
                        {
                            "expects_prompt": "login.name",
                            "gate_prompts": ["login.name"],
                            "id": "send_name",
                            "keys": "alice\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "login.password",
                            "gate_prompts": ["login.password"],
                            "id": "send_password",
                            "keys": "secret\r",
                            "kind": "send_keys",
                        },
                        {
                            "expects_prompt": "main.command",
                            "gate_prompts": ["main.command"],
                            "id": "done",
                            "kind": "noop",
                        },
                    ],
                }
            ],
            "game": "test",
            "prompts": [
                {
                    "id": "login.name",
                    "input_type": "multi_key",
                    "kv_extract": [{"field": "attempt", "regex": "Attempt\\s+(\\d+)", "type": "int"}],
                    "match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "login.password",
                    "input_type": "multi_key",
                    "match": {"match_mode": "contains", "pattern": "Enter password"},
                    "negative_match": {"match_mode": "contains", "pattern": "Enter your name"},
                },
                {
                    "id": "main.command",
                    "input_type": "single_key",
                    "match": {"match_mode": "contains", "pattern": "Command ["},
                },
            ],
            "version": "1.0",
        },
        "screen": "Enter your name:",
    },
    {
        "flow": "f",
        "name": "flow_ranks_by_position",
        "rules": {
            "flows": [
                {
                    "description": "x",
                    "id": "f",
                    "steps": [
                        {"gate_prompts": ["p0"], "id": "s0", "keys": "0\r", "kind": "send_keys"},
                        {"gate_prompts": ["p1"], "id": "s1", "keys": "1\r", "kind": "send_keys"},
                    ],
                }
            ],
            "game": "test",
            "prompts": [
                {"id": "p0", "input_type": "single_key", "match": {"match_mode": "contains", "pattern": "ZZZ"}},
                {"id": "p1", "input_type": "single_key", "match": {"match_mode": "contains", "pattern": "WWW"}},
            ],
            "version": "1.0",
        },
        "screen": "xxWWWxxxxZZZ",
    },
    {
        "flow": "f",
        "name": "flow_anchored_over_suffix",
        "rules": {
            "flows": [
                {
                    "description": "x",
                    "id": "f",
                    "steps": [
                        {"gate_prompts": ["anchored"], "id": "s0", "keys": "anchored\r", "kind": "send_keys"},
                        {"gate_prompts": ["suffix"], "id": "s1", "keys": "suffix\r", "kind": "send_keys"},
                    ],
                }
            ],
            "game": "test",
            "prompts": [
                {
                    "id": "anchored",
                    "input_type": "multi_key",
                    "match": {"match_mode": "regex", "pattern": "Enter your password:\\s*$"},
                },
                {
                    "id": "suffix",
                    "input_type": "multi_key",
                    "match": {"match_mode": "regex", "pattern": "password[?:]\\s*$"},
                },
            ],
            "version": "1.0",
        },
        "screen": "Enter your password: ",
    },
]

IDLE_CASES: list[dict[str, Any]] = [
    {
        "name": "idle_stable",
        "now": 105.0,
        "screens": [
            {"captured_at": 100.0, "screen": "s", "screen_hash": "same"},
            {"captured_at": 103.0, "screen": "s", "screen_hash": "same"},
        ],
        "threshold": 2.0,
    },
    {
        "name": "idle_changing",
        "now": 105.0,
        "screens": [
            {"captured_at": 100.0, "screen": "s1", "screen_hash": "h1"},
            {"captured_at": 104.5, "screen": "s2", "screen_hash": "h2"},
        ],
        "threshold": 2.0,
    },
    {"name": "idle_empty", "now": 105.0, "screens": [], "threshold": 0.0},
    {
        "name": "idle_below_threshold",
        "now": 105.0,
        "screens": [
            {"captured_at": 104.0, "screen": "s", "screen_hash": "same"},
            {"captured_at": 104.0, "screen": "s", "screen_hash": "same"},
        ],
        "threshold": 2.0,
    },
]

INPUT_TYPE_CASES: list[dict[str, Any]] = [
    {"screen": "Press any key to continue"},
    {"screen": "Hit any key"},
    {"screen": "-- more --"},
    {"screen": "<more> text"},
    {"screen": "Continue? (y/n)"},
    {"screen": "Quit?"},
    {"screen": "(q)uit"},
    {"screen": "[y/n]"},
    {"screen": "Please enter your choice"},
    {"screen": "Password: "},
    {"screen": "Command: "},
    {"screen": "random text no phrase"},
]


def detector_expected(case: dict[str, Any]) -> dict[str, Any]:
    patterns = case.get("patterns")
    if case.get("rules"):
        patterns = load_ruleset(json.dumps(case["rules"])).to_prompt_patterns()
    match = PromptDetector(patterns or []).detect_prompt(case["snapshot"])
    if match is None:
        return {"matched": False}
    kv_data: dict[str, Any] = {}
    if match.kv_extract:
        extracted = extract_kv(case["snapshot"].get("screen", ""), match.kv_extract)
        if extracted is not None:
            kv_data = extracted
    return {
        "matched": True,
        "prompt_id": match.prompt_id,
        "input_type": match.input_type,
        "kv_data": kv_data,
    }


def flow_expected(case: dict[str, Any]) -> dict[str, Any]:
    engine = FlowEngine(load_ruleset(json.dumps(case["rules"])))
    cursor = tuple(case["cursor"]) if case.get("cursor") else None
    step = engine.advance(case["flow"], case.get("screen", ""), cursor)
    return {
        "current_prompt_id": step.current_prompt_id,
        "next_action": step.next_action,
        "done": step.done,
        "kv_data": step.kv_data,
    }


def idle_expected(case: dict[str, Any]) -> dict[str, Any]:
    # Both the stamp and the staleness comparison read time.time().
    with patch("provide.uterm.detection.buffer.time.time", return_value=case["now"]):
        manager = BufferManager(50)
        for screen in case["screens"]:
            manager.add_screen(screen)
        return {"is_idle": manager.detect_idle_state(case["threshold"])}


def build() -> dict[str, Any]:
    return {
        "detector_cases": [{**c, "expected": detector_expected(c)} for c in DETECTOR_CASES],
        "flow_cases": [{**c, "expected": flow_expected(c)} for c in FLOW_CASES],
        "idle_cases": [{**c, "expected": idle_expected(c)} for c in IDLE_CASES],
        "input_type_cases": [{**c, "expected": auto_detect_input_type(c["screen"])} for c in INPUT_TYPE_CASES],
    }


def main() -> None:
    corpus = build()
    out = pathlib.Path(__file__).with_name("differential_golden.json")
    # indent=2, key-sorted, and NO trailing newline: this corpus's own style.
    out.write_text(json.dumps(corpus, indent=2, sort_keys=True), encoding="utf-8")
    total = sum(len(v) for v in corpus.values())
    print(f"wrote {out} ({total} cases across {len(corpus)} families)")


if __name__ == "__main__":
    main()
