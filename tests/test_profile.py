"""Profile schema, finalization gate, atomic store."""

from __future__ import annotations

import json

import pytest

from shared.profile.store import load_profile, save_profile
from shared.schemas.profile import Profile


def _complete_profile() -> Profile:
    return Profile.model_validate({
        "identity": {"first_name": "Sachin", "last_name": "T", "email": "s@example.com"},
        "experience": [{"company": "Acme", "title": "Engineer", "bullets": ["Built X"]}],
        "finalized": False,
    })


def test_finalized_blocked_when_incomplete():
    p = Profile()
    with pytest.raises(ValueError, match="Cannot set finalized=true"):
        p.finalized = True
        Profile.model_validate(p.model_dump())


def test_finalized_allowed_when_complete():
    p = _complete_profile()
    p.finalized = True
    Profile.model_validate(p.model_dump())   # should not raise


def test_extra_fields_rejected():
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        Profile.model_validate({"identity": {"first_name": "X"}, "hacker": "yes"})


def test_atomic_save_and_history(fresh_db):
    p = _complete_profile()
    save_profile(p, source="manual", notes="first")
    loaded = load_profile()
    assert loaded is not None
    assert loaded.identity.first_name == "Sachin"

    from shared.profile.store import list_history
    rows = list_history()
    assert len(rows) == 1
    assert rows[0].version == 1

    save_profile(p, source="manual", notes="second")
    assert len(list_history()) == 2


def test_json_roundtrip_no_data_loss(fresh_db):
    p = _complete_profile()
    save_profile(p, source="manual")
    raw = json.loads(load_profile().model_dump_json())
    assert raw["identity"]["email"] == "s@example.com"