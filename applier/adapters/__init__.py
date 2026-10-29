"""Adapter registry: which spec handles which application domain."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from applier.adapters.form import FormAdapter
from applier.adapters.spec import AdapterSpec

BUILTIN_SPECS = Path(__file__).parent / "specs"


def builtin_spec(name: str) -> AdapterSpec:
    return AdapterSpec.load(BUILTIN_SPECS / f"{name}.json")


def spec_for_url(url: str) -> AdapterSpec | None:
    """The built-in spec whose domain the URL's host ends with."""
    host = urlparse(url).netloc.lower()
    for path in sorted(BUILTIN_SPECS.glob("*.json")):
        spec = AdapterSpec.load(path)
        if host == spec.domain or host.endswith("." + spec.domain):
            return spec
    return None


def adapter_for_url(url: str) -> FormAdapter | None:
    spec = spec_for_url(url)
    return FormAdapter(spec) if spec else None
