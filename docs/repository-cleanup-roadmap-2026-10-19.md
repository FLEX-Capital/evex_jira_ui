# Repository cleanup roadmap

Date: 2026-10-19; implementation checkpoint: 2026-10-20.
All 33 approved phase 1–8 tasks are implemented locally. Phases 0–7 have independent
reviews; final phase 8 review is pending. The [verification report](repository-cleanup-verification-2026-10-20.md)
contains actual checks, every I01–I40/M01 disposition, review decisions and deferred
minors. Deployed I02 acceptance and the explicit staging checklist remain pending.

The [original repository review](repository-review-2026-10-19.md) is historical
evidence. The [development guide](development.md) contains the durable supported
architecture/setup contract, and the [task index](repository-cleanup-task-index-2026-10-20.md)
maps all phases, dependencies and issue owners.

Detailed approved local specifications, plans and phase review working files remain
optional plain paths under `docs/superpowers/`. They are ignored and have not been
force-added. A fresh clone can use the tracked guides and final report without them.

## Recommended delivery approach

Use nine ordered phases, with separately reviewable changes within each phase.
Correctness fixes precede broad movement of code. Every issue has exactly one
owning phase; later phases may consume its result without reopening its scope.
Each phase must remain independently usable, have a focused regression check,
and preserve the approved import/concurrency behavior.

Alternative orders considered: establish all shared infrastructure first, or make
one integrated cleanup branch. The proposed order gives useful results earlier
and limits the rollback surface; its cost is touching some callers more than once.

The existing Azure deployment work, asset relocation, and approved unified import
implementation are the foundation to preserve. The observed working branch is
`feature/azure-container-apps`, with staged and unstaged changes. At execution,
capture that foundation before creating an isolated workspace; starting from
HEAD alone would omit required current changes. Do not absorb unrelated edits
into cleanup commits.

## Decisions included in the design

- Keep the existing application stack and flat root module layout.
- Preserve documented desk/date policies, background imports, narrow safe cache
  updates, generic relationship compatibility, and country fallback behavior.
- Add explicit unknown-duration and overflow groups so charts account for their
  cohort; one normalized completion rule drives all views.
- Read application configuration only from process environment; inject local
  `.env` through the launcher and require network credentials lazily.
- Keep one authored dependency graph and generate the production export from it.
- Use explicit backed-up cache migrations; no automatic real-data rewrites.
- Refresh countries only during maintenance, with proposed seven-day successful
  and one-day no-country freshness and an explicit force option.
- Retain the ten newest recognized backups and all backups younger than 14 days;
  cleanup defaults to dry run and protects active recovery files.
- Version classifier results by input/configuration identity and merge writes
  transactionally. No paid model calls occur as a side effect of migration.
- Keep local tests, CI/image validation, and real ingress/storage verification
  distinct. Production rollout remains a separate operation.

## Phase sequence

| Phase | Deliverable                                                  | Findings owned                         | Prerequisites                   |
| ----- | ------------------------------------------------------------ | -------------------------------------- | ------------------------------- |
| 0     | Repository output protection and authentication revocation   | I01, I02, I18                          | Review current dirty foundation |
| 1     | Trustworthy time, completion, clone, and recency metrics     | I03, I04, I05, I06, I07                | Phase 0                         |
| 2     | Shared settings, desk/field policy, and dependency contract  | I21, I22, I25, I26, I33, I37           | Phase 1                         |
| 3     | Atomic persistence, schema validation, and safe retention    | I27, I29, I38                          | Phase 2                         |
| 4     | Shared country/source/link enrichment with explicit failures | I08, I09, I10, I11, I12, I17, I28, I30 | Phases 1–3                      |
| 5     | Validated, version-aware, concurrency-safe classifier        | I13, I14, I15, I16                     | Phases 2–3                      |
| 6     | Shared figures and focused dashboard views                   | I23, I24, I31                          | Phases 1–4                      |
| 7     | Maintained, reproducible analysis and historical archive     | I19, I20, I32                          | Phases 1–5                      |
| 8     | Enforced quality, lean runtime image, and final verification | I34, I35, I36, I39, I40                | Phases 0–7                      |

This order is the default execution sequence. Phase 5 does not technically depend
on phase 4, but overlapping configuration/persistence changes make sequential
implementation the simpler starting point. Agent-based execution is a later
choice; it is not assumed by this roadmap.

### Phase 0 Repository output protection and authentication

**Change:** strip tracked notebook code outputs/execution counts; add a safe
stdlib output checker to CI; invalidate authenticated sessions after password
rotation; preserve the already fixed CORS/XSRF command.

**Own:** four root notebooks, `tools/notebook_outputs.py`, `ui_auth.py`, focused
auth/notebook/container-contract tests, and the CI notebook-check invocation.
Use the phase 0 spec for exact behavior. Do not rework Azure resources or cache
formats here.

**Exit:** no saved code outputs in tracked notebooks; a synthetic ticket table is
rejected without printing its contents; rotation blocks data loading until a new
login; all existing authentication behavior and the safe container command pass.
Record I02 as locally verified, with deployed proxy verification still pending.

### Phase 1 Analytics correctness

**Change:** validate individual date/recency inputs, preserve other valid rows,
separate unknown duration from zero, add the `> 504` overflow group, normalize
completion, parse UTC recency, and create normalized clone-only relationship
reporting while retaining legacy generic-link fields.

**Own:** `data_transformation.py`, `resolution_bands.py`, `jira_loader.py`,
`service_desks.py`, a focused new `issue_links.py`, metric/relationship policy,
and the minimal affected chart consumers in `app.py`/`plotting.py`.

**Exit:** review probes R3, R4, R5, R10, and R15 have maintained regression cases.
Include mixed timezone offsets, same-instant ties, bad dates, 504-hour boundaries,
940-hour resolutions, localized status labels, mixed link types/directions, and
generic-link fixture compatibility. Chart totals reconcile to the stated cohort.
An invalid row makes the report incomplete rather than improving a KPI or
discarding all otherwise valid tickets for a desk.

### Phase 2 Shared configuration and dependencies

**Change:** introduce validated settings with a deterministic data/asset root;
remove application `.env` readers and import-time credential snapshots; put desk,
field, reporting, and tenant policy behind one authority. Keep credentials lazy
and the phase 0 authentication generation intact. Split analysis/dev dependencies,
remove unused/redundant packages, declare direct imports, and generate the locked
runtime requirements.

**Own:** new `settings.py`/`reporting_policy.py`, existing desk/client modules,
their call sites, `pyproject.toml`, generated `requirements.runtime.txt`, dependency
documentation, and lock/export tooling. Check all consumers before removing the
independent `requirements.txt`. Any lock change uses the supported package workflow
and preserves unrelated dependency constraints.

**Exit:** cached UI works with no Jira/model credentials; missing credentials fail
at the requested operation; all entry points agree on settings and paths from a
foreign working directory; every desk has exactly one transformation policy.
The generated runtime export matches the lock and a clean runtime import check.
Do not loosen the documented Streamlit/PyGWalker constraint.

### Phase 3 Durable storage contracts

**Change:** move neutral atomic-file behavior into `file_io.py`; remove checkpoint
imports of persistence from their importer; add centralized ticket schema
validation/migration and an explicit dry-run retention command. Preserve stable
sidecar locks, conditional saves, backup-before-change, narrow patches, and
network calls outside ticket-cache transactions.

**Own:** `file_io.py`, `cache_schema.py`, `cache_maintenance.py`, `data_loading.py`,
`sync_checkpoints.py`, and format-specific persistence callers. Introduce versioned
read/write compatibility before allowing any explicit migration write.

**Exit:** synthetic legacy, absent, corrupt, future-version, and mixed-dtype caches
have distinct outcomes; interrupted writes never replace a valid artifact; threads
and processes retain concurrent changes. Retention preserves its ten-backup/14-day
minimums, current snapshots, and active/interrupted logs, rejects symlinks, and
only applies after explicit selection. Do not exercise it on real `data/`.

### Phase 4 Shared enrichment policy

**Change:** make country resolution a shared module rather than a dependency of
an Excel command; unify reference selection, workspace identity, escalation,
cache freshness, date windows, response validation, retries, and incomplete
outcomes. Make linked-issue fetching explicit and deduplicated before deterministic
transformation. Standardize safe failure categories without losing optional-step
and per-desk isolation.

**Own:** `asset_country.py`, new `country_cache.py`/`http_policy.py`,
`jira_country_export.py`, `backfill_country.py`, `jira_loader.py`, `source_sync.py`,
`category_sync.py`, link enrichment, and affected import reports/UI feedback.

**Exit:** probes R1, R2, R6, R13, and R14 become maintained tests. Dashboard/export
agree on dates, first-reference selection, legacy provenance, and foreign-workspace
rejection. A 120-second Retry-After is honored or explicitly deferred, never
retried early. Missing country differs from failed lookup; CLI reports incomplete
work nonzero. Explicit maintenance can update a changed successful country.
Pure transformation makes no network calls and shared link targets are fetched
once per intended operation. Existing fallback and category-repair tests remain.

### Phase 5 Classification integrity

**Change:** constrain taxonomy and parent/sub-bucket combinations; normalize pandas
missing scalars and isolate invalid rows; fingerprint rendered input and classifier
configuration; merge versioned cache results under a shared transaction using
unique temporary files. Preserve one selected result per ticket for callers.

**Own:** `ticket_classifier.py`, new `classification_cache.py`, and classifier
schema/text/cache tests using a fake model client.

**Exit:** probes R7, R8, R9, and R11 have maintained coverage. Changing deployment,
prompt, taxonomy/schema, renderer, or relevant generation settings invalidates
reuse; identical identities reuse results. Independent process writers retain
both result sets, different configurations do not masquerade as each other, and
one `pd.NA` record does not abort unrelated valid work. No live classification or
implicit paid reclassification is part of migration validation.

### Phase 6 Dashboard and chart simplification

**Change:** share chart aggregation/figure behavior, keep widget state in focused
views, move maintenance transactions out of the entry point, share empty-state
rendering, and remove verified unused code. Scope CSS and keep the PyGWalker lock.

**Own:** `chart_data.py`, `plotting.py`, `dashboard_views.py`, `maintenance_ui.py`,
`app.py`, `styles.py`, and the specifically confirmed unused symbols. Preserve
external/notebook compatibility adapters with known consumers.

**Exit:** figure tests verify counts, percentages, order, unknown groups, log
mode, and totals across callers; AppTest checks independent widget keys, empty
states, authentication, imports, maintenance, and revision reloads. Capture a
synthetic before/after rerun measurement; do not introduce a cache only to meet
a subjective performance claim. The entry point no longer owns repeated chart
implementations or storage transactions.

### Phase 7 Analysis reproducibility

**Change:** retain one supported AI-analysis notebook using the authoritative
category policy and an explicit post-load project/date filter. Archive stale
experiments with status/provenance; preserve the reproducible historical deck
and its aggregate source snapshot. Record cohort and classifier identities.

**Own:** the maintained notebook, `analysis/archive/` and its index, historical
deck provenance, analysis smoke fixtures, and the dependency/docs adjustments
needed by that supported workflow.

**Exit:** out-of-window cached tickets do not change the cohort; modern unresolved
categories cannot borrow legacy labels; synthetic top-to-bottom notebook execution
needs no external access or paid calls. All notebooks obey phase 0 output policy.
The old board snapshot remains reproducible and clearly separate from live results.

### Phase 8 Quality and runtime delivery

**Change:** complete public typing/docstrings, make Ruff the Python formatter and
import authority, enforce the maintained type/lint scope, close documentation
links/setup contradictions, build a runtime allowlist image, and validate that
runtime in CI. Guard both Requests and HTTPX in offline tests.

**Own:** maintained production/test interfaces, `pyproject.toml`, `.trunk/`,
`.github/workflows/ci.yml`, runtime manifests, `Dockerfile`, `.dockerignore`,
README/development docs, focused quality/image contract checks, and final review
dispositions. Coordinate with the existing Azure deployment owner before editing
shared packaging files.

**Exit:** reproducible full-suite, configured lint/type, dependency-export parity,
notebook, docs-link, clean runtime installation, and built-image checks pass.
Application statement coverage remains at least 80%, with analysis separately
reported. Image contents contain all required runtime modules/assets and exclude
tests, analysis, tooling, secrets, and generated ticket data. No permanent blanket
ignore hides unresolved maintained production debt.

The final local report still lists staging checks for actual ingress, password
rotation, Azure Files locks/atomic replacement, restart/resume, and data durability.
Those require explicit staging execution and cannot be closed from mocks.

## Issue coverage ledger

Every review finding has one owner. Multiple findings can be solved by one shared
change, but each retains its original evidence and acceptance outcome.

| Finding | Owner   | Closure evidence                                                          |
| ------- | ------- | ------------------------------------------------------------------------- |
| I01     | Phase 0 | Clean tracked notebooks and a non-disclosing output guard                 |
| I02     | Phase 0 | Safe local command verified; deployed acceptance tracked separately       |
| I03     | Phase 1 | Invalid dates neither crash valid desk rows nor fabricate fast resolution |
| I04     | Phase 1 | Overflow/unknown durations reconcile chart cohorts                        |
| I05     | Phase 1 | All views share normalized completion                                     |
| I06     | Phase 1 | Clone-only reports with preserved generic-link compatibility              |
| I07     | Phase 1 | UTC recency and deterministic missing/tie behavior                        |
| I08     | Phase 4 | Berlin inclusive export end and current-date default                      |
| I09     | Phase 4 | Shared first-reference/provenance selection                               |
| I10     | Phase 4 | Lookup failure differs from no-country and returns incomplete status      |
| I11     | Phase 4 | Numeric/date Retry-After and retry classification tests                   |
| I12     | Phase 4 | Error-only/malformed source pages fail visibly                            |
| I13     | Phase 5 | Configuration/input fingerprint invalidates stale reuse                   |
| I14     | Phase 5 | Multiprocess merge retains independent results                            |
| I15     | Phase 5 | Allowed values and parent/sub-bucket membership enforced                  |
| I16     | Phase 5 | Nullable scalar and per-record failure tests                              |
| I17     | Phase 4 | Freshness/force refresh updates successful country values explicitly      |
| I18     | Phase 0 | Rotation, legacy-session, logout, and Unicode authentication tests        |
| I19     | Phase 7 | Maintained analysis honors normal-workspace category policy               |
| I20     | Phase 7 | Project/date cohort applied after load and recorded                       |
| I21     | Phase 2 | One authored locked graph with checked runtime export                     |
| I22     | Phase 2 | Environment-only settings and consistent lazy validation                  |
| I23     | Phase 6 | Shared deterministic chart construction and figure parity                 |
| I24     | Phase 6 | Focused views/maintenance and lean orchestration entry point              |
| I25     | Phase 2 | Desk registry drives all supported policies/callers                       |
| I26     | Phase 2 | One field/reporting/tenant policy authority                               |
| I27     | Phase 3 | Neutral atomic primitives and acyclic checkpoint dependency               |
| I28     | Phase 4 | Deduplicated explicit link enrichment and pure transformation             |
| I29     | Phase 3 | Central versioned validation and explicit legacy migration                |
| I30     | Phase 4 | Safe structured diagnostics distinguish failure modes                     |
| I31     | Phase 6 | Caller inventory and removal of confirmed unused code                     |
| I32     | Phase 7 | Supported analysis smoke path and documented historical archive           |
| I33     | Phase 2 | Minimal direct runtime graph and explicit analysis/dev groups             |
| I34     | Phase 8 | Maintained production interfaces pass enforced typing                     |
| I35     | Phase 8 | Defined formatter/lint scope passes without hidden production debt        |
| I36     | Phase 8 | CI exercises clean runtime and actual built image                         |
| I37     | Phase 2 | All paths work from a different working directory                         |
| I38     | Phase 3 | Dry-run/apply retention tests protect active artifacts                    |
| I39     | Phase 8 | Valid links and one coherent startup/test/dependency contract             |
| I40     | Phase 8 | Runtime copy allowlist verified against built-image contents              |

## Validation and rollout rules

Use TDD for behavior changes: add the focused failing case, verify the failure,
implement the smallest change, then run its focused checks and relevant existing
regressions. Do not add tests that merely mirror trivial code movement. Run the
full suite at each completed phase or when a shared contract changes, with all
storage redirected to temporary directories.

Phase 0 reran the full offline suite: 242 tests and 127 subtests pass, with 82%
statement coverage. Later phases must capture a fresh foundation and verification
results; these results do not establish future or deployed behavior.

For each delivered phase, record: owned files, issue dispositions, focused/full
test results, affected configuration/data format, and rollback requirements.
Only stage owned validated files. Keep behavior corrections, broad formatting,
and production deployment in independently reviewable changes.

Cache/format transitions require a backed-up explicit migration and a paired
application rollback. Never run old/new writers together against one cache
directory. Validate real storage semantics on staging before enabling additional
replicas or separate import jobs. Production data rebuild, reclassification,
retention application, Git history cleanup, push/merge, and deployment remain
separately invoked actions.

## Superpowers handoff

- [x] Read the review, current code/config changes, standards, and approved import design.
- [x] Classify the work as architectural and compare implementation approaches.
- [x] Write the program design, first subsystem spec, and complete issue roadmap.
- [x] Self-review issue coverage, dependency order, compatibility choices, and document links.
- [x] User reviews and approves the written design and phase 0 spec.
- [x] Use `superpowers:writing-plans` to produce the phase 0 task plan with exact interfaces, failing tests, commands, and focused commits.
- [x] User approves execution of the detailed phase 0 plan; use recommended native execution with one independent final review.
- [x] Implement phase 0 and verify both important review corrections with failing-then-passing regression tests and a green full suite.
- [ ] Resolve the existing Trunk notebook adapter commit blocker without restoring outputs or changing notebook source.
- [x] Prepare detailed plans for phases 1–8 against the approved program contracts: 33 tasks cover the 37 remaining original findings and M01.
- [ ] Review the detailed task plans, then execute phase 1 using the preserved native method.

Native execution and review are complete for phase 0. Repository policy ignores generated
`docs/superpowers/` artifacts; those local specifications have not been force-added
or committed. This roadmap is outside that ignored directory.
