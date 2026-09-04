"""State must outlive the process. Proven with a real second interpreter."""

import json
import os
import subprocess
import sys

from devsupervisor.state import machine
from tests.support import ROOT, HarnessTestCase

READER = """
import json, sys
sys.path.insert(0, {root!r})
from devsupervisor.state import Store
store = Store.open()
job = store.get_job({job_id!r})
print(json.dumps({{
    "status": job["status"],
    "branch": job["branch"],
    "criteria": job["acceptance_criteria"],
    "transitions": [t["to_status"] for t in store.transitions({job_id!r})],
    "unmet": store.unmet_dependencies({dependent!r}),
    "projects": [p["name"] for p in store.list_projects()],
}}))
"""


class RestartTests(HarnessTestCase):
    def test_state_survives_process_restart(self):
        project = self.make_project(name="restartable")
        goal = self.store.create_goal(project["id"], "Ship it")
        job = self.store.create_job(
            project["id"], "feature", "build", "widget", goal_id=goal["id"],
            branch="feat/widget", acceptance_criteria=["verify.sh green"],
        )
        dependent = self.store.create_job(
            project["id"], "feature", "review", "widget", goal_id=goal["id"],
            depends_on=[job["id"]],
        )
        self.store.transition(job["id"], machine.READY, actor="scheduler")
        self.store.transition(job["id"], machine.DISPATCHED, actor="scheduler")

        # Close every handle: the next process must read only what is on disk.
        self.store.close()

        environment = dict(os.environ)
        result = subprocess.run(
            [sys.executable, "-c", READER.format(
                root=str(ROOT), job_id=job["id"], dependent=dependent["id"])],
            capture_output=True, text=True, env=environment, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)

        self.assertEqual(observed["status"], machine.DISPATCHED)
        self.assertEqual(observed["branch"], "feat/widget")
        self.assertEqual(observed["criteria"], ["verify.sh green"])
        self.assertEqual(observed["transitions"],
                         [machine.PLANNED, machine.READY, machine.DISPATCHED])
        self.assertEqual(observed["unmet"], [job["id"]])
        self.assertEqual(observed["projects"], ["restartable"])

        # Reopen for the cleanup hook.
        from devsupervisor.state import Store
        self.store = Store.open()

    def test_reopening_is_idempotent_and_does_not_duplicate_schema_rows(self):
        from devsupervisor.state import Store
        self.store.close()
        for _ in range(3):
            store = Store.open()
            store.close()
        self.store = Store.open()
        versions = self.store.conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
        self.assertEqual(versions, 1)
