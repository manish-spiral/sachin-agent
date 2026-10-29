"""LLM provider resolution, extraction faithfulness and retry — offline."""

from __future__ import annotations

import pytest

from shared.config import reset_settings
from shared.llm import LLMNotConfigured, Provider, Task, resolve
from shared.profile import llm as extraction
from shared.profile.faithfulness import faithfulness_issues
from shared.schemas.profile import Profile

CV = """Priya Sharma | priya@example.com
Experience
Backend Engineer, Flipkart (Jan 2021 - Present)
Education: B.Tech, VIT Vellore
Skills: Python, Go, PostgreSQL"""


@pytest.fixture
def env(monkeypatch):
    for key in ("OPENAI_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER", "SCORING_MODEL", "EXTRACTION_MODEL"):
        monkeypatch.setenv(key, "")

    def apply(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        reset_settings()

    apply()
    yield apply
    reset_settings()


def test_no_key_raises(env):
    with pytest.raises(LLMNotConfigured, match="OPENAI_API_KEY or GROQ_API_KEY"):
        resolve(Task.SCORING)


def test_groq_key_alone_selects_groq_with_reasoning(env):
    env(GROQ_API_KEY="gsk_test")
    scoring, extracting = resolve(Task.SCORING), resolve(Task.EXTRACTION)
    assert scoring.provider == Provider.GROQ
    assert scoring.model == "openai/gpt-oss-120b"
    assert scoring.request_options() == {"reasoning": {"effort": "low"}}
    assert extracting.request_options() == {"reasoning": {"effort": "medium"}}


def test_openai_preferred_when_both_keys_set(env):
    env(OPENAI_API_KEY="sk_test", GROQ_API_KEY="gsk_test")
    config = resolve(Task.SCORING)
    assert config.provider == Provider.OPENAI
    assert config.request_options() == {}


def test_explicit_provider_and_model_override(env):
    env(OPENAI_API_KEY="sk_test", GROQ_API_KEY="gsk_test", LLM_PROVIDER="groq", SCORING_MODEL="qwen/x")
    config = resolve(Task.SCORING)
    assert (config.provider, config.model) == (Provider.GROQ, "qwen/x")
    assert config.request_options() == {}  # unknown model: don't send reasoning params


def test_api_key_never_in_repr(env):
    env(GROQ_API_KEY="gsk_secret_value")
    assert "gsk_secret_value" not in repr(resolve(Task.SCORING))


# --- faithfulness ----------------------------------------------------------


def _profile(**overrides) -> Profile:
    data = {
        "experience": [{"company": "Flipkart", "title": "Backend Engineer"}],
        "education": [{"institution": "VIT Vellore"}],
        "skills": {"languages": ["Python", "Go"], "tools": ["PostgreSQL"]},
    }
    data.update(overrides)
    return Profile.model_validate(data)


def test_faithful_profile_has_no_issues():
    assert faithfulness_issues(_profile(), CV) == []


def test_invented_values_are_flagged():
    profile = _profile(
        experience=[{"company": "Google", "title": "Backend Engineer"}],
        skills={"languages": ["Python", "Rust"]},
    )
    issues = faithfulness_issues(profile, CV)
    assert any("experience[0].company 'Google'" in i for i in issues)
    assert any("'Rust'" in i for i in issues)
    assert not any("'Python'" in i for i in issues)


def test_dropped_sections_are_flagged():
    issues = faithfulness_issues(_profile(skills={}, education=[]), CV)
    assert any("Skills section" in i for i in issues)
    assert any("Education section" in i for i in issues)


def test_extraction_retries_until_faithful(env, monkeypatch):
    env(GROQ_API_KEY="gsk_test")
    drafts = iter([_profile(skills={}), _profile()])
    calls: list[int] = []

    def fake_once(client, config, cv_text):
        calls.append(1)
        return next(drafts)

    monkeypatch.setattr(extraction, "_extract_once", fake_once)
    profile = extraction.extract_profile_from_text(CV)

    assert len(calls) == 2
    assert profile.skills.languages == ["Python", "Go"]
    assert profile.finalized is False


def test_extraction_keeps_least_bad_draft(env, monkeypatch):
    env(GROQ_API_KEY="gsk_test")
    worst = _profile(skills={}, education=[])
    better = _profile(skills={})
    drafts = iter([worst, better, worst])
    monkeypatch.setattr(extraction, "_extract_once", lambda *a: next(drafts))

    profile = extraction.extract_profile_from_text(CV)

    assert profile.education  # the draft with one issue, not two
