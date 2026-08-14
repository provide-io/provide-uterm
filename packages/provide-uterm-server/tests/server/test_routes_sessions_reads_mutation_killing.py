#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/sessions.py``: annotate / analyze / snapshot / events / recording.

Third of three files repairing ``sessions.py``'s 46.12% (see
``test_routes_sessions_list_mutation_killing.py`` for the cause).

What is load-bearing here:

* **``recording_download`` is a path-traversal boundary.** The resolved file
  must be inside the configured recording directory; both the ``is_relative_to``
  containment check and the missing-config branch answer 404, and both are
  tested with a path that genuinely escapes rather than a happy-path download.
* **Recording reads use ``can_read_recording``, not ``can_read_session``.** A
  transcript is a stricter grant than seeing the session exists, so a mutation
  swapping the predicate hands session viewers the recorded keystrokes.
* **``snapshot`` passes the Request as the redaction recipient**, which is what
  redacts the frame to the requester's role. Dropping it returns the unredacted
  screen.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

MODULE = "provide.uterm.server.routes.sessions"
_SID = "s-1"


def _handler(name: str) -> Any:
    from provide.uterm.server.routes.sessions import session_capability_handlers

    return session_capability_handlers()[name]


def _principal(subject_id: str = "alice") -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject_id, roles=set())


def _authz(*, can_read: bool = True, can_mutate: bool = True, can_read_recording: bool = True) -> MagicMock:
    az = MagicMock(name="authz")
    az.can_read_session = AsyncMock(return_value=can_read)
    az.can_mutate_session = AsyncMock(return_value=can_mutate)
    az.can_read_recording = AsyncMock(return_value=can_read_recording)
    az.is_admin = AsyncMock(return_value=True)
    return az


def _registry(**methods: Any) -> MagicMock:
    reg = MagicMock(name="registry")
    reg.get_definition = AsyncMock(return_value=SimpleNamespace(session_id=_SID))
    reg.analyze_session = AsyncMock(return_value={"lines": 1})
    reg.last_snapshot = AsyncMock(return_value={"screen": "x"})
    reg.events = AsyncMock(return_value=[{"seq": 1}])
    reg.watch_session_events = AsyncMock(return_value={"events": []})
    reg.recording_meta = AsyncMock(return_value={"bytes": 1})
    reg.recording_entries = AsyncMock(return_value=[{"event": "send"}])
    reg.recording_path = AsyncMock(return_value=None)
    reg.get_runtime = MagicMock(return_value=SimpleNamespace(_logger=None))
    for name, value in methods.items():
        setattr(reg, name, value)
    return reg


def _request(
    *,
    registry: Any,
    authz_obj: Any = None,
    hub: Any = None,
    config: Any = None,
    principal: Any = None,
    client_host: str = "1.2.3.4",
) -> MagicMock:
    req = MagicMock(name="request")
    state: dict[str, Any] = {
        "uterm_registry": registry,
        "uterm_authz": authz_obj if authz_obj is not None else _authz(),
        "uterm_hub": hub if hub is not None else _hub(),
    }
    if config is not None:
        state["uterm_config"] = config
    req.app.state = SimpleNamespace(**state)
    req.state = SimpleNamespace(uterm_principal=principal if principal is not None else _principal())
    req.client = SimpleNamespace(host=client_host)
    return req


def _hub() -> MagicMock:
    hub = MagicMock(name="hub")
    hub.append_event = AsyncMock(return_value={"seq": 7})
    return hub


async def _call(capability: str, req: Any, *args: Any, **kwargs: Any) -> Any:
    with patch(f"{MODULE}.audit_event", MagicMock()):
        return await _handler(capability)(req, *args, **kwargs)


# ===========================================================================
# annotate_session
# ===========================================================================


async def _annotate(payload: dict[str, Any], *, registry: Any = None, hub: Any = None) -> tuple[Any, MagicMock, Any]:
    audit = MagicMock()
    reg = registry if registry is not None else _registry()
    the_hub = hub if hub is not None else _hub()
    req = _request(registry=reg, hub=the_hub)
    with patch(f"{MODULE}.audit_event", audit), patch(f"{MODULE}.time.time", return_value=1234.0):
        result = await _handler("sessions.annotate")(req, _SID, payload)
    return result, audit, the_hub


class TestAnnotateValidation:
    @pytest.mark.parametrize("label", ["", "   "])
    async def test_a_blank_label_is_a_400(self, label: str) -> None:
        with pytest.raises(HTTPException) as exc:
            await _annotate({"label": label})

        assert exc.value.status_code == 400
        assert exc.value.detail == "label is required"

    async def test_a_missing_label_is_a_400(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await _annotate({})

        assert exc.value.status_code == 400
        assert exc.value.detail == "label is required"

    @pytest.mark.parametrize("severity", ["info", "warning", "high", "critical"])
    async def test_every_allowed_severity_is_accepted(self, severity: str) -> None:
        result, _audit, _hub = await _annotate({"label": "x", "severity": severity})

        assert result == {"ts": 1234.0, "seq": 7}

    async def test_severity_defaults_to_info(self) -> None:
        _result, audit, _hub = await _annotate({"label": "x"})

        assert audit.call_args.kwargs["detail"]["severity"] == "info"

    async def test_an_unknown_severity_is_a_400_naming_it(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await _annotate({"label": "x", "severity": "catastrophic"})

        assert exc.value.status_code == 400
        assert exc.value.detail == "invalid severity: catastrophic"

    async def test_a_session_without_a_runtime_is_a_404(self) -> None:
        reg = _registry(get_runtime=MagicMock(return_value=None))

        with pytest.raises(HTTPException) as exc:
            await _annotate({"label": "x"}, registry=reg)

        assert exc.value.status_code == 404
        assert exc.value.detail == f"no active runtime for session: {_SID}"


class TestAnnotateEffects:
    async def test_the_annotation_carries_the_label_description_severity_and_author(self) -> None:
        _result, audit, hub = await _annotate({"label": "boom", "description": "d", "severity": "high"})

        expected = {
            "label": "boom",
            "description": "d",
            "severity": "high",
            "source": "agent",
            "principal": "alice",
        }
        assert hub.append_event.await_args.args == (_SID, "annotation", expected)
        assert audit.call_args.kwargs["detail"] == expected

    async def test_the_label_is_stripped(self) -> None:
        _result, _audit, hub = await _annotate({"label": "  boom  "})

        assert hub.append_event.await_args.args[2]["label"] == "boom"

    async def test_a_missing_description_becomes_empty(self) -> None:
        _result, _audit, hub = await _annotate({"label": "x"})

        assert hub.append_event.await_args.args[2]["description"] == ""

    async def test_the_recording_logger_receives_the_annotation_when_present(self) -> None:
        logger = SimpleNamespace(log_event=AsyncMock())
        reg = _registry(get_runtime=MagicMock(return_value=SimpleNamespace(_logger=logger)))

        _result, _audit, _hub = await _annotate({"label": "x"}, registry=reg)

        logger.log_event.assert_awaited_once()
        assert logger.log_event.await_args.args[0] == "annotation"

    async def test_a_runtime_without_a_logger_still_annotates(self) -> None:
        result, _audit, hub = await _annotate({"label": "x"})

        hub.append_event.assert_awaited_once()
        assert result["seq"] == 7

    async def test_the_sequence_comes_from_the_event_and_defaults_to_zero(self) -> None:
        seqless_hub = _hub()
        seqless_hub.append_event = AsyncMock(return_value={})

        result, _audit, _returned_hub = await _annotate({"label": "x"}, hub=seqless_hub)

        assert result == {"ts": 1234.0, "seq": 0}

    async def test_the_audit_names_the_event_principal_session_and_source(self) -> None:
        _result, audit, _hub = await _annotate({"label": "x"})

        assert audit.call_args.args == ("session.annotate",)
        assert audit.call_args.kwargs["principal"] == "alice"
        assert audit.call_args.kwargs["session_id"] == _SID
        assert audit.call_args.kwargs["source_ip"] == "1.2.3.4"


# ===========================================================================
# analyze / snapshot / events / watch
# ===========================================================================


class TestAnalyzeSession:
    async def test_the_analysis_is_wrapped_with_its_session_id(self) -> None:
        req = _request(registry=_registry())

        assert await _call("sessions.analyze", req, _SID) == {"session_id": _SID, "analysis": {"lines": 1}}

    async def test_a_vanished_session_is_a_404(self) -> None:
        reg = _registry(analyze_session=AsyncMock(side_effect=KeyError(_SID)))
        req = _request(registry=reg)

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.analyze", req, _SID)

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown session: {_SID}"

    async def test_an_unreadable_session_is_a_403(self) -> None:
        req = _request(registry=_registry(), authz_obj=_authz(can_read=False))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.analyze", req, _SID)

        assert exc.value.status_code == 403


class TestSnapshot:
    async def test_the_request_is_passed_as_the_redaction_recipient(self) -> None:
        """The output policy redacts to the RECIPIENT's role; without it the
        caller receives the unredacted screen."""
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.snapshot", req, _SID)

        reg.last_snapshot.assert_awaited_once_with(_SID, recipient=req, wait_ms=0)

    async def test_the_snapshot_is_returned_verbatim(self) -> None:
        req = _request(registry=_registry())

        assert await _call("sessions.snapshot", req, _SID) == {"screen": "x"}

    async def test_an_absent_snapshot_is_none_not_an_error(self) -> None:
        reg = _registry(last_snapshot=AsyncMock(return_value=None))
        req = _request(registry=reg)

        assert await _call("sessions.snapshot", req, _SID) is None

    async def test_reading_defaults_to_the_cache_and_never_polls_the_worker(self) -> None:
        """``wait_ms`` defaults to 0, so the ordinary read stays a cache read.

        A polling default would put a round trip to the worker behind every
        dashboard refresh.
        """
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.snapshot", req, _SID)

        assert reg.last_snapshot.await_args.kwargs["wait_ms"] == 0

    async def test_a_caller_can_ask_for_a_round_trip_to_the_worker(self) -> None:
        """``wait_ms`` is forwarded rather than swallowed: it is the only way a
        caller turns "the screen has not changed" into "the worker still answers"."""
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.snapshot", req, _SID, wait_ms=1500)

        assert reg.last_snapshot.await_args.kwargs["wait_ms"] == 1500


class TestEvents:
    async def test_the_limit_is_forwarded(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.events", req, _SID, limit=42)

        reg.events.assert_awaited_once_with(_SID, limit=42)

    async def test_the_default_limit_is_one_hundred(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.events", req, _SID)

        reg.events.assert_awaited_once_with(_SID, limit=100)

    async def test_an_unreadable_session_is_a_403(self) -> None:
        req = _request(registry=_registry(), authz_obj=_authz(can_read=False))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.events", req, _SID)

        assert exc.value.status_code == 403


class TestWatchEvents:
    async def test_every_parameter_is_forwarded(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        await _call(
            "sessions.events_watch",
            req,
            _SID,
            timeout_ms=1500,
            event_types="send,read",
            pattern="boom",
            max_events=7,
        )

        reg.watch_session_events.assert_awaited_once_with(
            _SID,
            timeout_ms=1500,
            event_types=["send", "read"],
            pattern="boom",
            max_events=7,
        )

    async def test_the_event_type_filter_is_split_on_commas(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.events_watch", req, _SID, event_types="a,b,c")

        assert reg.watch_session_events.await_args.kwargs["event_types"] == ["a", "b", "c"]

    async def test_a_single_event_type_is_a_one_element_list(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.events_watch", req, _SID, event_types="send")

        assert reg.watch_session_events.await_args.kwargs["event_types"] == ["send"]

    async def test_no_event_type_filter_is_none_not_an_empty_list(self) -> None:
        """``[""]`` would filter every event out instead of matching all."""
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.events_watch", req, _SID)

        assert reg.watch_session_events.await_args.kwargs["event_types"] is None

    async def test_an_empty_event_type_string_is_also_none(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.events_watch", req, _SID, event_types="")

        assert reg.watch_session_events.await_args.kwargs["event_types"] is None

    async def test_the_defaults_are_forwarded(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.events_watch", req, _SID)

        assert reg.watch_session_events.await_args.kwargs == {
            "timeout_ms": 5000,
            "event_types": None,
            "pattern": None,
            "max_events": 50,
        }


# ===========================================================================
# recording meta / entries
# ===========================================================================


class TestRecordingReads:
    async def test_recording_meta_is_returned(self) -> None:
        req = _request(registry=_registry())

        assert await _call("sessions.recording", req, _SID) == {"bytes": 1}

    async def test_recording_meta_requires_the_recording_grant(self) -> None:
        """A transcript is a stricter grant than seeing the session exists."""
        req = _request(registry=_registry(), authz_obj=_authz(can_read_recording=False))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording", req, _SID)

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    async def test_recording_meta_404s_for_a_vanished_session(self) -> None:
        reg = _registry(recording_meta=AsyncMock(side_effect=KeyError(_SID)))
        req = _request(registry=reg)

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording", req, _SID)

        assert exc.value.detail == f"unknown session: {_SID}"

    async def test_recording_entries_forwards_every_filter(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.recording_entries", req, _SID, limit=5, offset=10, event="send")

        reg.recording_entries.assert_awaited_once_with(_SID, limit=5, offset=10, event="send")

    async def test_recording_entries_defaults(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        await _call("sessions.recording_entries", req, _SID)

        reg.recording_entries.assert_awaited_once_with(_SID, limit=200, offset=None, event=None)

    async def test_recording_entries_requires_the_recording_grant(self) -> None:
        req = _request(registry=_registry(), authz_obj=_authz(can_read_recording=False))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording_entries", req, _SID)

        assert exc.value.status_code == 403

    async def test_recording_entries_404s_for_a_vanished_session(self) -> None:
        reg = _registry(recording_entries=AsyncMock(side_effect=KeyError(_SID)))
        req = _request(registry=reg)

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording_entries", req, _SID)

        assert exc.value.detail == f"unknown session: {_SID}"


# ===========================================================================
# recording_download — containment boundary
# ===========================================================================


class TestRecordingDownload:
    async def test_a_file_inside_the_configured_directory_is_served(self, tmp_path: Path) -> None:
        recording = tmp_path / "rec" / f"{_SID}.json"
        recording.parent.mkdir()
        recording.write_text("{}")
        reg = _registry(recording_path=AsyncMock(return_value=recording))
        req = _request(registry=reg, config=SimpleNamespace(recording=SimpleNamespace(directory=tmp_path / "rec")))

        response = await _call("sessions.recording_download", req, _SID)

        assert isinstance(response, FileResponse)
        assert Path(response.path) == recording
        assert response.filename == f"{_SID}.json"
        assert response.media_type == "application/json"

    async def test_a_path_escaping_the_directory_is_refused(self, tmp_path: Path) -> None:
        """The containment check is the point of this handler's 404s: without it
        a registry-supplied path could serve any file the process can read."""
        outside = tmp_path / "etc" / "secrets.json"
        outside.parent.mkdir()
        outside.write_text("{}")
        allowed = tmp_path / "rec"
        allowed.mkdir()
        reg = _registry(recording_path=AsyncMock(return_value=outside))
        req = _request(registry=reg, config=SimpleNamespace(recording=SimpleNamespace(directory=allowed)))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording_download", req, _SID)

        assert exc.value.status_code == 404
        assert exc.value.detail == "recording not available"

    async def test_a_traversal_path_that_resolves_outside_is_refused(self, tmp_path: Path) -> None:
        """Resolved, not compared literally: `rec/../etc/x.json` is inside the
        directory as a string and outside it as a path."""
        allowed = tmp_path / "rec"
        allowed.mkdir()
        (tmp_path / "etc").mkdir()
        escaping = allowed / ".." / "etc" / "secrets.json"
        escaping.resolve().write_text("{}")
        reg = _registry(recording_path=AsyncMock(return_value=escaping))
        req = _request(registry=reg, config=SimpleNamespace(recording=SimpleNamespace(directory=allowed)))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording_download", req, _SID)

        assert exc.value.status_code == 404

    async def test_no_recording_path_is_a_404(self) -> None:
        req = _request(registry=_registry(), config=SimpleNamespace(recording=SimpleNamespace(directory=Path("/x"))))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording_download", req, _SID)

        assert exc.value.status_code == 404
        assert exc.value.detail == "recording not available"

    async def test_a_missing_file_is_a_404(self, tmp_path: Path) -> None:
        reg = _registry(recording_path=AsyncMock(return_value=tmp_path / "gone.json"))
        req = _request(registry=reg, config=SimpleNamespace(recording=SimpleNamespace(directory=tmp_path)))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording_download", req, _SID)

        assert exc.value.status_code == 404

    async def test_an_unconfigured_recording_directory_is_a_404(self, tmp_path: Path) -> None:
        """No configured root means nothing can be proven contained."""
        recording = tmp_path / f"{_SID}.json"
        recording.write_text("{}")
        reg = _registry(recording_path=AsyncMock(return_value=recording))
        req = _request(registry=reg, config=SimpleNamespace(recording=None))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording_download", req, _SID)

        assert exc.value.status_code == 404
        assert exc.value.detail == "recording not available"

    async def test_a_vanished_session_is_a_404_naming_it(self) -> None:
        reg = _registry(recording_path=AsyncMock(side_effect=KeyError(_SID)))
        req = _request(registry=reg)

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording_download", req, _SID)

        assert exc.value.detail == f"unknown session: {_SID}"

    async def test_downloads_require_the_recording_grant(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(can_read_recording=False))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.recording_download", req, _SID)

        assert exc.value.status_code == 403
        reg.recording_path.assert_not_awaited()
