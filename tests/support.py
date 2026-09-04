"""Shared test scaffolding.

Every test points DEVSUPERVISOR_HOME at a temp directory, so the suite can never
touch the user's real runtime root, and never performs network I/O.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from devsupervisor import clock, config  # noqa: E402
from devsupervisor.state import Store  # noqa: E402


class HarnessTestCase(unittest.TestCase):
    """A fresh runtime root and a fresh database per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="devsup-test-")
        self.home = Path(self._tmp.name)
        self._prior_home = os.environ.get(config.ENV_HOME)
        os.environ[config.ENV_HOME] = str(self.home)
        config.ensure_home()
        self.store = Store.open()
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.store.close()
        clock.unfreeze()
        if self._prior_home is None:
            os.environ.pop(config.ENV_HOME, None)
        else:
            os.environ[config.ENV_HOME] = self._prior_home
        self._tmp.cleanup()

    # --- fixtures ---------------------------------------------------------

    def make_project(self, name="demo", repo_path=None, policy_pack=None):
        project = self.store.create_project(
            name, repo_path or str(self.home / "repo"), policy_pack=policy_pack
        )
        config.ensure_project_dirs(project["id"])
        return project

    def make_goal(self, project=None, title="Build feature X", **kwargs):
        project = project or self.make_project()
        return self.store.create_goal(project["id"], title, **kwargs)

    def make_job(self, project=None, goal=None, subject="widget", role="build",
                 job_type="feature", **fields):
        project = project or self.make_project()
        return self.store.create_job(
            project["id"], job_type, role, subject,
            goal_id=(goal or {}).get("id"), **fields
        )

    def drive(self, job_id, path, actor="worker-1", fields=None):
        """Walk a job through a list of states, asserting nothing along the way."""
        job = None
        for status in path:
            job = self.store.transition(job_id, status, actor=actor, fields=fields)
        return job
