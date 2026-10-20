# Cleanup phases and task index

Date: 20 October 2026. Status: approved task plans in execution.

**8 phase plans · 33 tasks · 33 implemented locally · local dispositions for I01–I40 and M01; staging remains separate.**

Phase 1 is independently reviewed and passes 285 tests/127 subtests at 83%
coverage. See its verification (optional local file `docs/superpowers/verification/2026-10-20-repository-cleanup-phase-1.md`).
Phase 2 is independently reviewed: 314 passed, 127 subtests, 84% coverage;
see its verification (optional local file `docs/superpowers/verification/2026-10-20-repository-cleanup-phase-2.md`).
Phase 3 is independently reviewed: 377 tests pass, 127 subtests, 85% coverage;
see its verification (optional local file `docs/superpowers/verification/2026-10-20-repository-cleanup-phase-3.md`).
Phase 4 is independently reviewed: 458 passed, 127 subtests, 87% coverage;
see its verification (optional local file `docs/superpowers/verification/2026-10-20-repository-cleanup-phase-4.md`).
Phase 5 is independently reviewed and corrected: 547 passed, 127 subtests, 87% coverage;
see its verification (optional local file `docs/superpowers/verification/2026-10-20-repository-cleanup-phase-5.md`).
Phase 6 is independently reviewed: 567 passed, 127 subtests, 88% coverage;
see its verification (optional local file `docs/superpowers/verification/2026-10-20-repository-cleanup-phase-6.md`).
Phase 7 is independently reviewed and corrected: 592 passed, 127 subtests, 93% combined coverage;
see its verification (optional local file `docs/superpowers/verification/2026-10-20-repository-cleanup-phase-7.md`).
Phase 8 implementation is complete; its independent final review is pending. See the
[final verification report](repository-cleanup-verification-2026-10-20.md) for application-only
coverage, runtime integration and issue dispositions. The shared execution contract
and protected phase 0/Azure foundation still apply.

## Phase overview

| Phase | Plan                                                                                                                                    | Tasks | Original findings                      | Prerequisite                                                                                                               |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------- | ----- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 1     | Analytics correctness (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-1.md`)                           | 5     | I03, I04, I05, I06, I07                | Phase 0 working-tree package and its preserved foundation.                                                                 |
| 2     | Shared configuration and locked dependencies (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-2.md`)    | 4     | I21, I22, I25, I26, I33, I37           | Phase 1 interfaces and normalized columns are present.                                                                     |
| 3     | Atomic persistence schema and retention (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-3.md`)         | 4     | I27, I29, I38                          | Phase 2 settings/paths and phase 1 normalized fields.                                                                      |
| 4     | Shared enrichment and explicit failures (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-4.md`)         | 5     | I08, I09, I10, I11, I12, I17, I28, I30 | Phases 1–3; consume the shared settings, field registry, file I/O and schema interfaces.                                   |
| 5     | Classification integrity (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-5.md`)                        | 3     | I13, I14, I15, I16                     | Phases 2–3; phase 4 safe-diagnostic helper is available in the default sequential order.                                   |
| 6     | Dashboard and chart simplification (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-6.md`)              | 4     | I23, I24, I31                          | Phases 1–4 interfaces; retain phase 0 auth and fragment guards.                                                            |
| 7     | Reproducible maintained analysis (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-7.md`)                | 3     | I19, I20, I32                          | Phases 1–5; use normalized cache/category provenance and selected classifier identities.                                   |
| 8     | Quality runtime delivery and final verification (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-8.md`) | 5     | I34, I35, I36, I39, I40, M01           | Phases 0–7, including the local phase 0 notebook/tool/CI package; review current Azure packaging edits before integration. |

Use the default phase order 1→2→3→4→5→6→7→8. Technical independence does
not automatically authorize parallel execution: several phases share settings,
storage, transformation, UI or packaging files. P5 also consumes diagnostics from
P4.4 in this sequential plan. If commits remain blocked, only the minimal notebook
adapter repair slice of P8.1 may be pulled forward; log it as partial, without
starting the broad formatting/type sweep or marking all of P8.1 complete.

## Task backlog

| Task                                                                                                                                                                   | Deliverable                                                        | Findings                | Execution status    |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ----------------------- | ------------------- |
| P1.1 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-1.md#task-1-normalize-completion-and-time-quality`)                              | Normalize completion and time quality                              | I03, I05                | Complete locally    |
| P1.2 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-1.md#task-2-order-raw-duplicate-snapshots-by-utc-recency`)                       | Order raw duplicate snapshots by UTC recency                       | I07                     | Complete locally    |
| P1.3 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-1.md#task-3-isolate-invalid-creation-dates-and-report-incomplete-desks`)         | Isolate invalid creation dates and report incomplete desks         | I03                     | Complete locally    |
| P1.4 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-1.md#task-4-separate-normalized-clone-records-from-generic-links`)               | Separate normalized clone records from generic links               | I06                     | Complete locally    |
| P1.5 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-1.md#task-5-reconcile-duration-cohorts-and-all-completion-consumers`)            | Reconcile duration cohorts and all completion consumers            | I04, I05                | Complete locally    |
| P2.1 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-2.md#task-1-load-validated-environment-settings-and-construct-clients-lazily`)   | Load validated environment settings and construct clients lazily   | I22                     | Complete locally    |
| P2.2 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-2.md#task-2-route-mutable-data-and-static-assets-through-deterministic-paths`)   | Route mutable data and static assets through deterministic paths   | I37                     | Complete locally    |
| P2.3 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-2.md#task-3-consolidate-desk-field-and-reporting-policy`)                        | Consolidate desk, field and reporting policy                       | I25, I26                | Complete locally    |
| P2.4 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-2.md#task-4-lock-runtime-dependencies-and-separate-analysis-tooling`)            | Lock runtime dependencies and separate analysis tooling            | I21, I33                | Complete locally    |
| P3.1 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-3.md#task-1-introduce-neutral-durable-writes-and-path-locks`)                    | Introduce neutral durable writes and path locks                    | I27                     | Complete locally    |
| P3.2 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-3.md#task-2-validate-and-normalize-versioned-ticket-frames`)                     | Validate and normalize versioned ticket frames                     | I29                     | Complete locally    |
| P3.3 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-3.md#task-3-provide-explicit-revision-guarded-ticket-migration`)                 | Provide explicit revision-guarded ticket migration                 | I29                     | Complete locally    |
| P3.4 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-3.md#task-4-plan-and-apply-conservative-retention-under-owning-locks`)           | Plan and apply conservative retention under owning locks           | I38                     | Complete locally    |
| P4.1 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-4.md#task-1-share-response-validation-and-bounded-retry-policy`)                 | Share response validation and bounded retry policy                 | I11, I12                | Complete locally    |
| P4.2 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-4.md#task-2-centralize-provenance-aware-country-resolution`)                     | Centralize provenance-aware country resolution                     | I09, I10                | Complete locally    |
| P4.3 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-4.md#task-3-version-country-cache-and-refresh-explicitly`)                       | Version country cache and refresh explicitly                       | I17, I10                | Complete locally    |
| P4.4 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-4.md#task-4-make-link-enrichment-explicit-and-diagnostics-safe`)                 | Make link enrichment explicit and diagnostics safe                 | I28, I30                | Complete locally    |
| P4.5 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-4.md#task-5-align-country-export-dates-outcomes-and-maintenance-adapters`)       | Align country export dates outcomes and maintenance adapters       | I08, I09, I10, I30      | Complete locally    |
| P5.1 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-5.md#task-1-validate-taxonomy-and-normalize-nullable-ticket-text`)               | Validate taxonomy and normalize nullable ticket text               | I15, I16                | Complete locally    |
| P5.2 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-5.md#task-2-persist-versioned-classification-identities-transactionally`)        | Persist versioned classification identities transactionally        | I13, I14                | Complete locally    |
| P5.3 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-5.md#task-3-wire-lazy-classification-reuse-and-selected-results`)                | Wire lazy classification reuse and selected results                | I13, I14, I16           | Complete locally    |
| P6.1 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-6.md#task-1-share-deterministic-chart-data-and-figure-construction`)             | Share deterministic chart data and figure construction             | I23                     | Complete locally    |
| P6.2 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-6.md#task-2-extract-authenticated-maintenance-transactions`)                     | Extract authenticated maintenance transactions                     | I24                     | Complete locally    |
| P6.3 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-6.md#task-3-extract-focused-dashboard-views-and-scoped-styles`)                  | Extract focused dashboard views and scoped styles                  | I23, I24                | Complete locally    |
| P6.4 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-6.md#task-4-remove-verified-dead-implementations-and-retain-called-adapters`)    | Remove verified dead implementations and retain called adapters    | I31                     | Complete locally    |
| P7.1 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-7.md#task-1-extract-cohort-selection-and-provenance-into-a-maintained-workflow`) | Extract cohort selection and provenance into a maintained workflow | I19, I20                | Implemented locally |
| P7.2 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-7.md#task-2-archive-obsolete-notebooks-and-freeze-board-provenance`)             | Archive obsolete notebooks and freeze board provenance             | I32                     | Implemented locally |
| P7.3 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-7.md#task-3-execute-the-maintained-notebook-offline-top-to-bottom`)              | Execute the maintained notebook offline top to bottom              | I19, I20, I32           | Implemented locally |
| P8.1 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-8.md#task-1-unify-python-quality-tooling-and-close-notebook-guard-gaps`)         | Unify Python quality tooling and close notebook guard gaps         | I35, M01                | Implemented locally |
| P8.2 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-8.md#task-2-complete-maintained-typing-and-public-documentation`)                | Complete maintained typing and public documentation                | I34, I35                | Implemented locally |
| P8.3 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-8.md#task-3-make-supported-setup-and-local-documentation-links-reliable`)        | Make supported setup and local documentation links reliable        | I39                     | Implemented locally |
| P8.4 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-8.md#task-4-build-and-exercise-an-explicit-runtime-allowlist-image`)             | Build and exercise an explicit runtime allowlist image             | I36, I40                | Implemented locally |
| P8.5 (optional local file `docs/superpowers/plans/2026-10-20-repository-cleanup-phase-8.md#task-5-enforce-ci-gates-and-reconcile-every-issue-disposition`)             | Enforce CI gates and reconcile every issue disposition             | I36, I34, I35, I39, I40 | Implemented locally |

Each task has exact owned files and interfaces, named assertions with a seed
test, a focused command and expected result, integration notes, affected-file
checks, and an owned conventional-commit boundary. P6.4 is behavior-preserving
dead-code removal: it uses a passing behavioral baseline rather than manufactured
RED or source-deletion tests. Full-suite/review gates belong at every phase exit.

## Cross-phase interface ledger

| Producer  | Contract                                                        | Consumers                                              |
| --------- | --------------------------------------------------------------- | ------------------------------------------------------ |
| P1.1/P1.5 | completion_state, duration_quality, normalized completion/bands | P1.3, P3.2, P6.1/P6.3, P7.1                            |
| P1.4      | IssueLink, clone_links, normalized relationship columns         | P3.2, P4.4, P6.3, P7.1                                 |
| P2.1/P2.2 | Settings, TenantIdentity, DataPaths, lazy credentials           | All later I/O/entry-point tasks                        |
| P2.3      | ServiceDesk policy and JiraFields/FIELDS                        | Shared extraction/enrichment/export and UI/CLI choices |
| P2.4      | Locked runtime export and explicit runtime-install test option  | P6.4, P8.4/P8.5                                        |
| P3.1      | atomic_write, write_json_atomic, path_lock, Revision            | P3.2–P3.4, P4.3, P5.2, P6.2                            |
| P3.2      | v2 frame schema/validator, explicit cache errors                | P3.3, P4.2, P6.3, P7.1                                 |
| P4.1      | Response validation and operation-specific retry budgets        | P4.2–P4.5                                              |
| P4.2/P4.3 | AssetIdentity, typed country outcomes, CountryCache v2          | P4.5, P6.2                                             |
| P4.4      | LinkGraph and safe OperationDiagnostic                          | P4.5, P5.1/P5.3, P6.2                                  |
| P5.2/P5.3 | ClassifierConfig/identity, transactional selected results       | P7.1/P7.3                                              |
| P6.1      | ChartData and pure figure builder                               | P6.3                                                   |
| P7.1      | AnalysisSpec, selected cohort and provenance manifest           | P7.3                                                   |
| P8.1      | Native mandatory Ruff gate, safe notebook validation            | P8.2/P8.5 and preserved P0 commit                      |

## Finding-to-task coverage

| Finding | Owning task(s)   | Status                                                |
| ------- | ---------------- | ----------------------------------------------------- |
| I03     | P1.1, P1.3       | Fixed locally; 285-test reviewed phase 1              |
| I04     | P1.5             | Fixed locally; 285-test reviewed phase 1              |
| I05     | P1.1, P1.5       | Fixed locally; 285-test reviewed phase 1              |
| I06     | P1.4             | Fixed locally; 285-test reviewed phase 1              |
| I07     | P1.2             | Fixed locally; 285-test reviewed phase 1              |
| I08     | P4.5             | Fixed locally; 458-test reviewed phase 4              |
| I09     | P4.2, P4.5       | Fixed locally; 458-test reviewed phase 4              |
| I10     | P4.2, P4.3, P4.5 | Fixed locally; 458-test reviewed phase 4              |
| I11     | P4.1             | Fixed locally; 458-test reviewed phase 4              |
| I12     | P4.1             | Fixed locally; 458-test reviewed phase 4              |
| I13     | P5.2, P5.3       | Fixed locally; 547-test reviewed phase 5              |
| I14     | P5.2, P5.3       | Fixed locally; 547-test reviewed phase 5              |
| I15     | P5.1             | Fixed locally; 547-test reviewed phase 5              |
| I16     | P5.1, P5.3       | Fixed locally; 547-test reviewed phase 5              |
| I17     | P4.3             | Fixed locally; 458-test reviewed phase 4              |
| I19     | P7.1, P7.3       | Implemented locally; evidence required before closure |
| I20     | P7.1, P7.3       | Implemented locally; evidence required before closure |
| I21     | P2.4             | Fixed locally; 314-test reviewed phase 2              |
| I22     | P2.1             | Fixed locally; 314-test reviewed phase 2              |
| I23     | P6.1, P6.3       | Fixed locally; 567-test reviewed phase 6              |
| I24     | P6.2, P6.3       | Fixed locally; 567-test reviewed phase 6              |
| I25     | P2.3             | Fixed locally; 314-test reviewed phase 2              |
| I26     | P2.3             | Fixed locally; 314-test reviewed phase 2              |
| I27     | P3.1             | Fixed locally; 377-test reviewed phase 3              |
| I28     | P4.4             | Fixed locally; 458-test reviewed phase 4              |
| I29     | P3.2, P3.3       | Fixed locally; 377-test reviewed phase 3              |
| I30     | P4.4, P4.5       | Fixed locally; 458-test reviewed phase 4              |
| I31     | P6.4             | Fixed locally; 567-test reviewed phase 6              |
| I32     | P7.2, P7.3       | Implemented locally; evidence required before closure |
| I33     | P2.4             | Fixed locally; 314-test reviewed phase 2              |
| I34     | P8.2, P8.5       | Implemented locally; evidence required before closure |
| I35     | P8.1, P8.2, P8.5 | Implemented locally; evidence required before closure |
| I36     | P8.4, P8.5       | Implemented locally; evidence required before closure |
| I37     | P2.2             | Fixed locally; 314-test reviewed phase 2              |
| I38     | P3.4             | Fixed locally; 377-test reviewed phase 3              |
| I39     | P8.3, P8.5       | Implemented locally; evidence required before closure |
| I40     | P8.4, P8.5       | Implemented locally; evidence required before closure |
| M01     | P8.1             | Implemented locally; evidence required before closure |

I01/I02/I18 retain phase 0 ownership and local verification; deployed I02
acceptance and the notebook-package commit remain open as already recorded.
M01 is the additional malformed notebook cell_type diagnostic issue, owned by P8.1.

## Checks before execution

- [x] Map all remaining original IDs and M01 to named tasks with no cross-phase ownership gaps.
- [x] Specify all five review-focus cases per phase and point them to task tests.
- [x] Check producer/consumer signatures, path roots, explicit migration and compatibility behavior.
- [x] Verify Superpowers can extract each of the 33 task briefs independently.
- [x] Parse Python examples, validate local links/anchors, format Markdown and preserve source hashes.
- [x] Detailed plans approved before implementation.
- [x] Foundation captured and baseline rerun at phase entry; phase verification records carry current evidence.

## External acceptance kept separate

Actual ingress/CORS/XSRF/widgets and password rotation, Azure Files locks and
atomic replacement at one replica, interrupted resume/restarts, persistent data,
final hostname and SSO require explicit staging execution. No real-cache
migration, pruning/rebuild, paid classification, history rewrite or production
deployment is part of the local task execution described here.
