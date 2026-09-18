"""Filesystem layout of the persistent runtime root.

Everything durable lives under DEVSUPERVISOR_HOME (default ~/.devsupervisor) so
that a test can point the whole system at a temp directory and touch nothing of
the user's.
"""

import os
from pathlib import Path

ENV_HOME = "DEVSUPERVISOR_HOME"
DEFAULT_HOME = Path.home() / ".devsupervisor"

# Memory areas. Path-addressed and small beats one growing file.
MEMORY_AREAS = (
    "product",
    "architecture",
    "decisions",
    "conventions",
    "experiments",
    "incidents",
    "lessons",
    "orchestration",
)


def home():
    return Path(os.environ.get(ENV_HOME) or DEFAULT_HOME).expanduser()


def db_path():
    return home() / "supervisor.db"


def logs_dir():
    return home() / "logs"


def projects_dir():
    return home() / "projects"


def packs_dir():
    """Policy packs that belong to this machine rather than to the repository.

    A pack names a real project and its rules, so the shipped tree carries only
    a generic example; the packs a person actually runs live beside their data.
    """
    return home() / "packs"


def project_dir(project_id):
    return projects_dir() / project_id


def memory_dir(project_id):
    return project_dir(project_id) / "memory"


def memory_candidates_dir(project_id):
    return project_dir(project_id) / "memory-candidates"


def jobs_dir(project_id):
    return project_dir(project_id) / "jobs"


def job_dir(project_id, job_id):
    return jobs_dir(project_id) / job_id


def transcripts_dir(project_id):
    return project_dir(project_id) / "transcripts"


def artifacts_dir(project_id):
    return project_dir(project_id) / "artifacts"


def checkpoints_dir(project_id):
    return project_dir(project_id) / "checkpoints"


def ensure_home():
    """Create the runtime root. Safe to call repeatedly."""
    home().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    projects_dir().mkdir(parents=True, exist_ok=True)
    return home()


def ensure_project_dirs(project_id):
    """Create every directory a project needs, including memory areas."""
    for path in (
        project_dir(project_id),
        memory_candidates_dir(project_id),
        jobs_dir(project_id),
        transcripts_dir(project_id),
        artifacts_dir(project_id),
        checkpoints_dir(project_id),
    ):
        path.mkdir(parents=True, exist_ok=True)
    for area in MEMORY_AREAS:
        (memory_dir(project_id) / area).mkdir(parents=True, exist_ok=True)
    return project_dir(project_id)
