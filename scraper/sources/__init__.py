from scraper.sources.arbeitnow import ArbeitnowSource
from scraper.sources.ashby import AshbySource
from scraper.sources.base import BaseSource, HttpFetchError, RawJob
from scraper.sources.greenhouse import GreenhouseSource
from scraper.sources.lever import LeverSource
from scraper.sources.remoteok import RemoteOKSource
from scraper.sources.remotive import RemotiveSource
from scraper.sources.smartrecruiters import SmartRecruitersSource
from scraper.sources.workable import WorkableSource

SOURCE_REGISTRY: dict[str, type[BaseSource]] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "workable": WorkableSource,
    "smartrecruiters": SmartRecruitersSource,
    "remoteok": RemoteOKSource,
    "remotive": RemotiveSource,
    "arbeitnow": ArbeitnowSource,
}

__all__ = [
    "ArbeitnowSource",
    "AshbySource",
    "BaseSource",
    "GreenhouseSource",
    "HttpFetchError",
    "LeverSource",
    "RawJob",
    "RemoteOKSource",
    "RemotiveSource",
    "SOURCE_REGISTRY",
    "SmartRecruitersSource",
    "WorkableSource",
]
