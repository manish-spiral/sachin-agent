"""Canonical keys for titles, companies and locations. Pure functions, no I/O.

The keys are for matching and dedup, not display: the raw fields stay on Job.
"""

from __future__ import annotations

import re
import unicodedata

from shared.schemas.job import Job

_TITLE_TOKENS = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "eng": "engineer",
    "engr": "engineer",
    "engrg": "engineering",
    "mgmt": "management",
    "mgr": "manager",
    "assoc": "associate",
    "dir": "director",
    "vp": "vice president",
    "svp": "senior vice president",
    "swe": "software engineer",
    "sde": "software development engineer",
    "sre": "site reliability engineer",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "qa": "quality assurance",
    "ux": "user experience",
    "&": "and",
}
# Work-arrangement words say nothing about the role and vary by board.
_TITLE_DROP = {"remote", "hybrid", "onsite", "on-site"}
# German boards append gender markers: (m/w/d), (f/m/d), (w/m/div.), (all genders).
_GENDER_RE = re.compile(r"\((?:[mwfdx]\s*/\s*)+(?:[mwfdx]|div)\.?\)|\(all genders\)", re.I)
_TITLE_SPLIT_RE = re.compile(r"[\s,/|()\[\]:;–—_\-]+")

_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co",
    "plc", "gmbh", "ag", "sa", "bv", "nv", "pvt", "private", "srl", "oy", "ab",
}

_LOCATION_ALIASES = {
    "bangalore": "bengaluru",
    "blr": "bengaluru",
    "blore": "bengaluru",
    "bengaluru urban": "bengaluru",
    "bombay": "mumbai",
    "gurgaon": "gurugram",
    "new delhi": "delhi",
    "madras": "chennai",
    "calcutta": "kolkata",
    "poona": "pune",
    "nyc": "new york",
    "new york city": "new york",
    "sf": "san francisco",
    "bay area": "san francisco",
    "sf bay area": "san francisco",
    "san francisco bay area": "san francisco",
    "la": "los angeles",
    "dc": "washington",
    "washington dc": "washington",
    "washington d c": "washington",
    "munchen": "munich",
    "koln": "cologne",
    "wien": "vienna",
}
_REMOTE_WORDS = ("remote", "anywhere", "worldwide", "work from home", "wfh", "distributed")
_LOCATION_SPLIT_RE = re.compile(r"\s*(?:;|\||\n| / | or )\s*", re.I)
_PAREN_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[^\w\s]")


def _fold(text: str) -> str:
    """Lowercase and strip accents, so 'München' and 'Munchen' compare equal."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower().strip()


def normalize_title(title: str) -> str:
    text = _GENDER_RE.sub(" ", _fold(title)).replace(".", "")
    tokens: list[str] = []
    for token in _TITLE_SPLIT_RE.split(text):
        if not token or token in _TITLE_DROP:
            continue
        tokens.append(_TITLE_TOKENS.get(token, token))
    return " ".join(tokens)


def normalize_company(company: str) -> str:
    """Join tokens so a board slug ('huggingface') equals a display name ('Hugging Face')."""
    # Drop dots first so "S.A." and "Inc." become the suffix tokens "sa" / "inc".
    text = _PUNCT_RE.sub(" ", _fold(company).replace(".", "").replace("&", " and "))
    tokens = [t for t in text.split() if t not in _COMPANY_SUFFIXES]
    return "".join(tokens)


def _normalize_location_part(part: str) -> str | None:
    text = _fold(part)
    if any(word in text for word in _REMOTE_WORDS):
        return "remote"
    text = _PAREN_RE.sub(" ", text)
    text = re.sub(r"^(?:hybrid|on-?site|office)\s*[-:]?\s*", "", text)
    city = _PUNCT_RE.sub(" ", text.split(",")[0])
    city = " ".join(city.split())
    if not city:
        return None
    return _LOCATION_ALIASES.get(city, city)


def normalize_location(location_raw: str, remote: bool | None = None) -> str | None:
    """'Bangalore, KA; Remote (US)' → 'bengaluru | remote'. None when nothing is known."""
    parts: list[str] = []
    for chunk in _LOCATION_SPLIT_RE.split(location_raw or ""):
        part = _normalize_location_part(chunk)
        if part and part not in parts:
            parts.append(part)
    if remote and "remote" not in parts:
        parts.append("remote")
    return " | ".join(parts) or None


def apply_normalization(job: Job) -> Job:
    job.title_norm = normalize_title(job.title)
    job.company_norm = normalize_company(job.company)
    job.location_norm = normalize_location(job.location_raw, job.remote)
    return job
