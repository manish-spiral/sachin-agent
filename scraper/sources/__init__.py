from scraper.sources.ashby import AshbySource
from scraper.sources.base import BaseSource, HttpFetchError, RawJob
from scraper.sources.greenhouse import GreenhouseSource
from scraper.sources.lever import LeverSource

SOURCE_REGISTRY: dict[str, type[BaseSource]] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
}

__all__ = [
    "AshbySource",
    "BaseSource",
    "GreenhouseSource",
    "HttpFetchError",
    "LeverSource",
    "RawJob",
    "SOURCE_REGISTRY",
]