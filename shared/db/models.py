"""Re-export schema tables so SQLModel registers them on import."""

from shared.schemas.adapter import AdapterVersion
from shared.schemas.application import Application
from shared.schemas.consent import ConsentChoice
from shared.schemas.document import TailoredDocument
from shared.schemas.failure import FailureEvent
from shared.schemas.job import Job
from shared.schemas.pipeline_run import PipelineRun
from shared.schemas.profile_fact import ProfileFact
from shared.schemas.profile_record import ProfileRecord
from shared.schemas.skill import Skill
from shared.schemas.snapshot import SnapshotIndex
from shared.schemas.source_health import SourceHealth

__all__ = [
    "AdapterVersion",
    "Application",
    "ConsentChoice",
    "FailureEvent",
    "Job",
    "PipelineRun",
    "ProfileFact",
    "ProfileRecord",
    "Skill",
    "SnapshotIndex",
    "SourceHealth",
    "TailoredDocument",
]