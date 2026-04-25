from __future__ import annotations

from dataclasses import is_dataclass
from typing import Literal, get_type_hints

from provide.terminal.control.plane.approval import ApprovalRecord, ApprovalStore
from provide.terminal.control.plane.lease import LeaseRecord, LeaseStore
from provide.terminal.control.plane.session import SessionRecord, SessionStore
from provide.terminal.control.plane.token import ResumeTokenRecord, SessionTokenRecord, TokenStore


def test_portable_feature_contracts_exist() -> None:
    assert is_dataclass(SessionRecord)
    assert is_dataclass(SessionTokenRecord)
    assert is_dataclass(ResumeTokenRecord)
    assert is_dataclass(ApprovalRecord)
    assert is_dataclass(LeaseRecord)
    assert SessionStore.__name__ == "SessionStore"
    assert TokenStore.__name__ == "TokenStore"
    assert ApprovalStore.__name__ == "ApprovalStore"
    assert LeaseStore.__name__ == "LeaseStore"


def test_feature_records_use_portable_field_shapes() -> None:
    session_hints = get_type_hints(SessionRecord)
    token_hints = get_type_hints(SessionTokenRecord)
    resume_hints = get_type_hints(ResumeTokenRecord)
    approval_hints = get_type_hints(ApprovalRecord)
    lease_hints = get_type_hints(LeaseRecord)

    assert session_hints["session_id"] is str
    assert session_hints["deleted_at"] == float | None
    assert token_hints["token_kind"] is str
    assert token_hints["revoked_at"] == float | None
    assert resume_hints["was_hijack_owner"] is bool
    assert approval_hints["state"] == Literal["pending", "approved", "rejected"]
    assert lease_hints["deleted_at"] == float | None
