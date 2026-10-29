"""Phase 4: canonical title / company / location keys."""

from __future__ import annotations

import pytest

from scraper.normalize import (
    apply_normalization,
    normalize_company,
    normalize_location,
    normalize_title,
)
from shared.schemas.job import Job


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sr. Software Engineer, Backend", "senior software engineer backend"),
        ("Senior Software Engineer - Backend", "senior software engineer backend"),
        ("Jr. ML Eng", "junior machine learning engineer"),
        ("Growth Product Manager (f/m/d)", "growth product manager"),
        ("Servicetechniker (w/m/div.)", "servicetechniker"),
        ("Backend Engineer (Remote)", "backend engineer"),
        ("Director, Software Engrg Mgmt", "director software engineering management"),
        ("DevOps Engineer", "devops engineer"),
    ],
)
def test_normalize_title(raw: str, expected: str):
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Stripe, Inc.", "stripe"),
        ("stripe", "stripe"),
        ("Hugging Face", "huggingface"),
        ("huggingface", "huggingface"),
        ("7NXT GmbH", "7nxt"),
        ("AT&T", "atandt"),
        ("Bosch Group", "boschgroup"),
        ("Société Générale S.A.", "societegenerale"),
    ],
)
def test_normalize_company(raw: str, expected: str):
    assert normalize_company(raw) == expected


@pytest.mark.parametrize(
    ("raw", "remote", "expected"),
    [
        ("Bangalore, KA", None, "bengaluru"),
        ("Blr", None, "bengaluru"),
        ("blore", None, "bengaluru"),
        ("Bengaluru, Karnataka, India", None, "bengaluru"),
        ("Gurgaon", None, "gurugram"),
        ("New York, NY (HQ)", True, "new york | remote"),
        ("NYC", None, "new york"),
        ("SF Bay Area", None, "san francisco"),
        ("Remote - US", None, "remote"),
        ("Anywhere in the world", None, "remote"),
        ("San Francisco, CA; New York, NY", None, "san francisco | new york"),
        ("London | London", None, "london"),
        ("München", None, "munich"),
        ("Hybrid - London, UK", None, "london"),
        ("", True, "remote"),
        ("", None, None),
    ],
)
def test_normalize_location(raw: str, remote: bool | None, expected: str | None):
    assert normalize_location(raw, remote) == expected


def test_apply_normalization_fills_all_keys():
    job = Job(
        id="x",
        source="greenhouse:acme",
        external_id="1",
        title="Sr. Data Eng",
        company="Acme, Inc.",
        location_raw="Bombay",
    )
    apply_normalization(job)
    assert job.title_norm == "senior data engineer"
    assert job.company_norm == "acme"
    assert job.location_norm == "mumbai"
