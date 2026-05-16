#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from provide.uterm.tunnel.types import HttpRequestMessage, HttpResponseMessage


def test_http_request_message_fields():
    msg: HttpRequestMessage = {
        "type": "http_req",
        "id": "r1",
        "ts": 1711000000.0,
        "method": "GET",
        "url": "/api/users",
        "headers": {"accept": "application/json"},
        "body_size": 0,
    }
    assert msg["type"] == "http_req"
    assert msg["id"] == "r1"


def test_http_response_message_fields():
    msg: HttpResponseMessage = {
        "type": "http_res",
        "id": "r1",
        "ts": 1711000000.089,
        "status": 200,
        "status_text": "OK",
        "headers": {"content-type": "application/json"},
        "body_size": 18,
        "duration_ms": 89,
    }
    assert msg["status"] == 200
    assert msg["duration_ms"] == 89


def test_http_request_with_body():
    msg: HttpRequestMessage = {
        "type": "http_req",
        "id": "r2",
        "ts": 1711000000.0,
        "method": "POST",
        "url": "/api/login",
        "headers": {"content-type": "application/json"},
        "body_size": 42,
        "body_b64": "eyJ1c2VyIjoiYWRtaW4ifQ==",
    }
    assert msg["body_b64"] == "eyJ1c2VyIjoiYWRtaW4ifQ=="


def test_http_response_truncated():
    msg: HttpResponseMessage = {
        "type": "http_res",
        "id": "r3",
        "ts": 1.0,
        "status": 200,
        "status_text": "OK",
        "headers": {},
        "body_size": 300000,
        "duration_ms": 10,
        "body_truncated": True,
    }
    assert msg["body_truncated"] is True
