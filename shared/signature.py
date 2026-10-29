"""Structural fingerprints for snapshots: the contract behind
SnapshotIndex.page_signature and SnapshotIndex.features.

The signature identifies a page's *form structure* — which tags, roles,
control ids and buttons are present — not its content. It ignores counts and
question labels, so two jobs on the same ATS (with different custom
questions) share a signature, while a redesigned form gets a new one.

The feature vector keeps everything (bucketed counts, label words) and is what
similarity search compares. For API snapshots the same idea fingerprints the
JSON schema (key paths), so schema drift shows up as a signature change.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from html.parser import HTMLParser
from typing import Any

SIGNATURE_VERSION = "sig1"
_TRACKED_TAGS = {"form", "input", "select", "textarea", "button", "label", "iframe", "fieldset", "a"}
_MAX_ANCHORS = 40
_WORD_RE = re.compile(r"[a-z]{3,}")


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.counts: Counter[str] = Counter()
        self.anchors: Counter[str] = Counter()
        self._capture: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag in _TRACKED_TAGS:
            self.counts[f"tag:{tag}"] += 1
        if tag == "input":
            self.counts[f"type:{a.get('type', 'text').lower()}"] += 1
        if a.get("role"):
            self.counts[f"role:{a['role'].lower()}"] += 1
        for attr in ("id", "name"):
            # Stable control ids ("first_name") are strong anchors; generated
            # ones ("question_17919411004", "question_2") collapse to "question_#".
            if tag in {"input", "select", "textarea"} and a.get(attr):
                self.anchors[f"{attr}:{re.sub(r'[0-9]+', '#', a[attr].lower())}"] += 1
        if tag in {"label", "button", "legend"}:
            self._capture, self._text = tag, []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._capture:
            words = _WORD_RE.findall(" ".join(self._text).lower())[:4]
            if words:
                self.anchors[f"{tag}:{' '.join(words)}"] += 1
            self._capture = None


def _bucket(n: int) -> int:
    return int(math.log2(n)) + 1 if n > 0 else 0


def html_features(html: str) -> dict[str, float]:
    parser = _FormParser()
    parser.feed(html)
    features = {k: float(_bucket(v)) for k, v in parser.counts.items()}
    for anchor, _ in parser.anchors.most_common(_MAX_ANCHORS):
        features[f"anchor:{anchor}"] = 1.0
    return features


def json_features(payload: Any, *, max_depth: int = 4) -> dict[str, float]:
    """Key paths of a JSON document; list items are merged ("jobs[].title")."""
    paths: Counter[str] = Counter()

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                paths[f"key:{child_path}"] += 1
                walk(child, child_path, depth + 1)
        elif isinstance(value, list):
            for item in value[:50]:
                walk(item, f"{path}[]", depth + 1)

    walk(payload, "", 0)
    return {k: 1.0 for k in paths}


def page_signature(features: dict[str, float]) -> str:
    """Hash of which structural keys are present. Label words and counts are
    left out: they vary per job on the same ATS."""
    keys = sorted(k for k in features if not k.startswith("anchor:label:") and not k.startswith("anchor:legend:"))
    canonical = "|".join(keys)
    return f"{SIGNATURE_VERSION}:{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0
