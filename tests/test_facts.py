"""Profile fact store: the database every LLM claim must cite."""

from __future__ import annotations

import json

import pytest
from sqlmodel import select
from typer.testing import CliRunner

from shared.db.engine import get_session
from shared.paths import profile_json_path
from shared.profile.cli import app
from shared.profile.facts import (
    active_facts,
    format_facts_for_prompt,
    make_fact_id,
    unknown_citations,
)
from shared.profile.store import list_history, load_current_profile, save_profile
from shared.schemas.profile import Profile
from shared.schemas.profile_fact import FactKind, ProfileFact

CV = """Asha Rao | asha@example.com
Summary: Spark and Python engineer.
Experience
Data Engineer, Acme Corp (Mar 2020 - Present)
- Built a Spark pipeline processing 2 TB/day
Skills: Python, Spark"""


def _profile(**overrides) -> Profile:
    data: dict = {
        "identity": {"first_name": "Asha", "last_name": "Rao", "email": "asha@example.com",
                     "location": "Pune"},
        "work_authorization": {"requires_sponsorship": False, "authorized_countries": ["India"]},
        "preferences": {"target_titles": ["Data Engineer"], "salary_floor": 3000000},
        "experience": [{
            "company": "Acme Corp", "title": "Data Engineer", "start_date": "Mar 2020",
            "bullets": ["Built a Spark pipeline processing 2 TB/day"], "technologies": ["Spark"],
        }],
        "skills": {"languages": ["Python"], "tools": ["Spark"]},
        "answers": [{"question": "Notice period?", "answer": "30 days"}],
    }
    data.update(overrides)
    return Profile.model_validate(data)


def _all_facts() -> list[ProfileFact]:
    with get_session() as s:
        return list(s.exec(select(ProfileFact)).all())


def test_save_explodes_profile_into_facts(fresh_db):
    save_profile(_profile(), source_text=CV)
    by_path = {f.path: f for f in active_facts(include_pii=True)}

    assert by_path["experience[0]"].text == "Data Engineer at Acme Corp, Mar 2020 – Present"
    bullet = by_path["experience[0].bullets[0]"]
    assert bullet.kind == FactKind.EXPERIENCE_BULLET
    assert bullet.parent_id == by_path["experience[0]"].id
    assert by_path["work_authorization.requires_sponsorship"].text == (
        "Does not require visa sponsorship to work outside India"
    )
    assert by_path["answers[0]"].text == "Q: Notice period? A: 30 days"
    assert by_path["identity.email"].pii is True
    assert by_path["identity.location"].pii is False


def test_evidence_points_at_the_cv_line(fresh_db):
    save_profile(_profile(), source_text=CV)
    by_path = {f.path: f for f in active_facts(include_pii=True)}
    assert by_path["experience[0].bullets[0]"].evidence == "- Built a Spark pipeline processing 2 TB/day"
    assert by_path["experience[0]"].evidence == "Data Engineer, Acme Corp (Mar 2020 - Present)"
    assert by_path["preferences.salary_floor"].evidence is None  # not from the CV
    # A child fact cites a line inside its parent's section, not the summary.
    assert by_path["experience[0].technologies[0]"].evidence == "- Built a Spark pipeline processing 2 TB/day"


def test_ids_are_stable_across_saves_and_reordering(fresh_db):
    save_profile(_profile())
    first = {f.text: f.id for f in active_facts(include_pii=True)}

    reordered = _profile(skills={"tools": ["Spark"], "languages": ["Python"]})
    save_profile(reordered)
    second = {f.text: f.id for f in active_facts(include_pii=True)}

    assert first == second
    assert make_fact_id(FactKind.SKILL, "languages|python") == first["Skill (languages): Python"]


def test_removed_facts_are_deactivated_not_deleted(fresh_db):
    save_profile(_profile())
    old_bullet = next(f.id for f in active_facts() if f.kind == FactKind.EXPERIENCE_BULLET)

    edited = _profile()
    edited.experience[0].bullets = ["Cut pipeline cost by 40%"]
    save_profile(edited)

    stored = {f.id: f for f in _all_facts()}
    assert stored[old_bullet].active is False
    assert stored[old_bullet].last_profile_version == 1
    assert old_bullet not in {f.id for f in active_facts()}
    new_bullet = next(f for f in active_facts() if f.kind == FactKind.EXPERIENCE_BULLET)
    assert (new_bullet.first_profile_version, new_bullet.text) == (2, "At Acme Corp: Cut pipeline cost by 40%")


def test_verified_follows_finalization(fresh_db):
    draft = _profile()
    save_profile(draft)
    assert not any(f.verified for f in active_facts())

    final = _profile(finalized=True)
    save_profile(final)
    assert all(f.verified for f in active_facts())


def test_pii_never_reaches_a_prompt(fresh_db):
    save_profile(_profile())
    prompt = format_facts_for_prompt(active_facts())
    assert "asha@example.com" not in prompt and "Asha" not in prompt
    with pytest.raises(ValueError, match="PII"):
        format_facts_for_prompt(active_facts(include_pii=True))


def test_unknown_citations():
    shown = [make_fact_id(FactKind.SKILL, "x"), make_fact_id(FactKind.SKILL, "y")]
    assert unknown_citations([shown[0]], shown) == []
    assert unknown_citations([shown[0], "F0000000000", "garbage"], shown) == ["F0000000000", "garbage"]


def test_hand_edit_of_profile_json_is_versioned_and_synced(fresh_db):
    save_profile(_profile())
    data = json.loads(profile_json_path().read_text())
    data["skills"]["languages"].append("Scala")
    profile_json_path().write_text(json.dumps(data))

    current = load_current_profile()

    assert current is not None and current[1] == 2
    assert list_history(limit=1)[0].notes == "hand edit of profile.json"
    assert "Skill (languages): Scala" in {f.text for f in active_facts()}
    assert load_current_profile() == current  # unchanged file: no new version


def test_facts_cli(fresh_db):
    save_profile(_profile(), source_text=CV)
    result = CliRunner().invoke(app, ["facts", "--kind", "experience_bullet", "--evidence"])
    assert result.exit_code == 0, result.output
    assert "At Acme Corp: Built a Spark pipeline" in result.output
    assert "↳ CV: - Built a Spark pipeline" in result.output
    assert "1 facts" in result.output


def test_import_refuses_to_overwrite_existing_profile(fresh_db, tmp_path):
    save_profile(_profile())
    cv = tmp_path / "cv.txt"
    cv.write_text(CV)

    result = CliRunner().invoke(app, ["import", str(cv)])

    assert result.exit_code == 1
    assert "--force" in result.output
    assert len(list_history()) == 1


def test_fact_wording_changes_reach_the_db_on_load(fresh_db):
    save_profile(_profile())
    with get_session() as s:
        fact = s.exec(select(ProfileFact).where(ProfileFact.path == "identity.location")).one()
        fact.text = "stale wording from an older release"
        s.add(fact)
        s.commit()

    load_current_profile()

    assert "Based in Pune" in {f.text for f in active_facts()}
    assert len(list_history()) == 1  # same profile: no new version
