# Acceptance Criteria → Tests

The 30 bootstrap acceptance criteria, each mapped to the automated test that
proves it. A criterion with no test is not met.

| # | Criterion | Proven by |
|---|---|---|
| 1 | standalone clean git repo | `test_repo_hygiene.py::test_this_is_a_standalone_git_repository` |
| 2 | core is project-agnostic | `test_policy_packs.py::test_core_has_no_project_references` |
| 3 | project rules outside core | `test_policy_packs.py::test_example_rules_live_in_a_pack` |
| 4 | state survives restart | `test_state_restart.py::test_state_survives_process_restart` |
| 5 | deterministic validated transitions | `test_state_machine.py` (legal matrix, illegal rejection, guards) |
| 6 | all durable entities persist | `test_store.py::test_all_entities_round_trip` |
| 7 | handoffs reconciled without agent numbers | `test_handoff_import.py::test_dedup_ignores_agent_number` |
| 8 | packets scoped, not transcript dumps | `test_context_compiler.py::test_packet_excludes_transcripts` |
| 9 | artifacts carry work across context resets | `test_context_compiler.py::test_fresh_process_rebuilds_the_packet_from_artifacts_alone` |
| 10 | reviewer independence enforced | `test_review_independence.py`, `test_full_lifecycle.py::test_the_approver_is_the_reviewer_not_the_builder` |
| 11 | builder cannot self-approve | `test_review_independence.py::test_builder_cannot_approve_own_job` |
| 12 | rejection creates targeted revision | `test_full_lifecycle.py::test_rejection_revision_approval_landing_and_evaluation` |
| 13 | approved code routes to separate landing | `test_full_lifecycle.py::test_landing_is_a_separate_job_that_runs_after_approval` |
| 14 | goal completion checked after verification | `test_full_lifecycle.py::test_evaluator_runs_after_verify_and_closes_the_goal` |
| 15 | resume after crash without repeating work | `test_crash_resume.py::test_resume_picks_up_at_landing_without_repeating_completed_work` |
| 16 | task locks prevent duplicate execution | `test_leases.py`, `test_operations.py::SupervisorLockTests` |
| 17 | bounded retries | `test_full_lifecycle.py::RevisionCapTests` |
| 18 | human gates pause safely | `test_gates.py` |
| 19 | mock provider proves system, no paid calls | `test_full_lifecycle.py`, `test_no_paid_calls.py` |
| 20 | Claude adapter or documented blocker | `test_claude_adapter.py` (argv and parsing only; never invoked) |
| 21 | metrics and retrospectives work | `test_metrics.py`, `test_retrospective.py` |
| 22 | self-improvement yields candidates only | `test_retrospective.py::test_candidates_are_not_auto_adopted` |
| 23 | immutable rules cannot self-modify | `test_policy_immutable.py` |
| 24 | curation cannot silently overwrite | `test_memory_curation.py`, `test_memory.py::test_existing_document_is_never_silently_overwritten` |
| 25 | CLI supports normal use | `test_cli.py` |
| 26 | generic behaviour on a non-Example project | `test_sample_project.py` |
| 27 | Example handoffs imported and deduped | `test_handoff_import.py`, `test_example_dryrun.py::ExampleDryRunTests` |
| 28 | Example dry run produces a coherent graph | `test_example_dryrun.py::test_the_dry_run_offers_reviews_of_exact_shas` |
| 29 | no Example product code modified | `test_example_dryrun.py::ExampleDryRunTests._assert_repo_untouched` (HEAD, status, and every ref compared before and after) |
| 30 | full suite green | `scripts/test.sh` — 259 tests |

## Model-routing follow-up

| Requirement | Proven by |
|---|---|
| initial policy resolves to Opus at high thinking for every role | `test_model_routing.py::InitialPolicyTests` |
| the concrete model id is discovered, not hardcoded | `test_model_routing.py::ResolutionTests`, `::test_no_marketing_string_is_hardcoded_in_the_router` |
| critical roles cannot be silently downgraded | `test_model_routing.py::CriticalRoleFloorTests` |
| model and thinking are persisted in every run record | `test_model_routing.py::RunRecordTests` |
| experiment arms cannot diverge in configuration | `test_experiments.py::PairLockTests`, `::PairLockAtDispatchTests` |
| a worker-requested subtask is created by the supervisor | `test_delegation.py::AuthorizationTests`, `::EndToEndDelegationTests` |
| a retrospective cannot reach the routing floor | `test_model_routing.py::test_a_retrospective_cannot_reach_the_routing_floor` |


## Non-negotiables for the suite itself

- **Zero paid calls.** Every test uses the mock provider. `test_no_paid_calls.py`
  asserts the default provider resolution never returns a network provider
  without explicit configuration.
- **Zero network.** No test performs I/O outside `$DEVSUPERVISOR_HOME` (pointed
  at a temp dir) and its own fixtures.
- **No writes to any real project.** Example tests are read-only or operate on
  disposable fixture repos.
