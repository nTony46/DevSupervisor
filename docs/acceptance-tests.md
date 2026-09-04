# Acceptance Criteria → Tests

The 30 bootstrap acceptance criteria, each mapped to the automated test that
proves it. A criterion with no test is not met.

| # | Criterion | Proven by |
|---|---|---|
| 1 | standalone clean git repo | `tests/test_repo_hygiene.py` |
| 2 | core is project-agnostic | `test_policy_packs.py::test_core_has_no_project_references` |
| 3 | project rules outside core | `test_policy_packs.py::test_example_rules_live_in_pack` |
| 4 | state survives restart | `test_state_restart.py::test_state_survives_process_restart` |
| 5 | deterministic validated transitions | `test_state_machine.py` (legal matrix + illegal rejection) |
| 6 | all durable entities persist | `test_store.py::test_all_entities_round_trip` |
| 7 | handoffs reconciled without agent numbers | `test_handoff_import.py::test_dedup_ignores_agent_number` |
| 8 | packets scoped, not transcript dumps | `test_context_compiler.py::test_packet_excludes_transcripts` |
| 9 | artifacts carry work across context resets | `test_context_compiler.py::test_fresh_context_handoff_from_artifacts` |
| 10 | reviewer independence enforced | `test_review_independence.py` |
| 11 | builder cannot self-approve | `test_review_independence.py::test_builder_cannot_approve_own_job` |
| 12 | rejection creates targeted revision | `test_review_loop.py::test_rejection_creates_targeted_revision` |
| 13 | approved code routes to separate landing | `test_review_loop.py::test_landing_is_a_separate_job` |
| 14 | goal completion checked after verification | `test_full_lifecycle.py::test_evaluator_runs_after_verify` |
| 15 | resume after crash without repeating work | `test_crash_resume.py` |
| 16 | task locks prevent duplicate execution | `test_leases.py` |
| 17 | bounded retries | `test_review_loop.py::test_revision_cap_escalates` |
| 18 | human gates pause safely | `test_gates.py` |
| 19 | mock provider proves system, no paid calls | `test_full_lifecycle.py` + `test_no_paid_calls.py` |
| 20 | Claude adapter or documented blocker | `test_claude_adapter.py` (dry-run construction only) |
| 21 | metrics and retrospectives work | `test_metrics.py`, `test_retrospective.py` |
| 22 | self-improvement yields candidates only | `test_retrospective.py::test_candidates_are_not_auto_adopted` |
| 23 | immutable rules cannot self-modify | `test_policy_immutable.py` |
| 24 | curation cannot silently overwrite | `test_memory_curation.py` |
| 25 | CLI supports normal use | `test_cli.py` |
| 26 | generic behavior on a non-Example project | `test_sample_project.py` |
| 27 | Example handoffs imported + deduped | `test_handoff_import.py` |
| 28 | Example dry-run produces coherent graph | `test_example_dryrun.py` |
| 29 | no Example product code modified | `test_example_readonly.py` (dry-run makes no writes) |
| 30 | full suite green | `scripts/test.sh` |

## Non-negotiables for the suite itself

- **Zero paid calls.** Every test uses the mock provider. `test_no_paid_calls.py`
  asserts the default provider resolution never returns a network provider
  without explicit configuration.
- **Zero network.** No test performs I/O outside `$DEVSUPERVISOR_HOME` (pointed
  at a temp dir) and its own fixtures.
- **No writes to any real project.** Example tests are read-only or operate on
  disposable fixture repos.
