"""Global kill switch: a non-empty KILL_SWITCH env var, or a file named KILL in
the data directory. Long-running loops check it between units of work and
stop cleanly, saving state. The file form works on a process already running."""

from __future__ import annotations

import os
from pathlib import Path

from shared.config import get_settings


def kill_file() -> Path:
    return Path(get_settings().data_dir) / "KILL"


def is_killed() -> bool:
    # The env var is read fresh, not from cached settings, so tests and child
    # processes see the current value.
    return bool(os.environ.get("KILL_SWITCH") or kill_file().exists())
