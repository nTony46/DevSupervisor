# sample_project

A deliberately tiny, deliberately non-Example repository. It exists so the test
suite can prove DevSupervisor's core is project-agnostic: the same planner,
scheduler, review loop, and landing chain drive a project the harness has never
been told anything about.

It has one known defect (`average([])` raises) so a `bug` workflow has something
real to reproduce, diagnose, and fix.
