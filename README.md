# Jira Analytics Dashboard

Repository tooling is based on
[`flex-capital/flex_copier_template`](https://github.com/flex-capital/flex_copier_template)
v0.1.7. Copier answers are saved in `.copier-answers.yml`; update with
`copier update --trust --skip-tasks` and review the merge. The template is adapted
to this application's flat module layout and Python 3.12 Docker runtime.
Install analysis and development tools with `uv sync --frozen --group analysis --group dev`,
run tests and coverage with `uv run --frozen --no-sync pytest`, and install Git hooks with `trunk git-hooks sync`.

Streamlit analytics for the service desks on `amparex.atlassian.net`:

| Company | Project | Jira project ID        | Service desk ID        |
| ------- | ------- | ---------------------- | ---------------------- |
| Ipro    | SDIPR   | Existing configuration | Existing configuration |
| Amparex | SDAX    | Existing configuration | Existing configuration |
| Euronet | SDEU    | 12521                  | 219                    |

The company multiselect starts with all three companies selected. Choose any
combination to filter the analysis. Selecting none shows a prompt instead of charts.
The refresh button always fetches all three desks for the selected creation-date
window; it reports each desk separately and retains cached tickets if a desk fails.

## Run locally

Use Python 3.12 and install the locked dashboard environment:

```sh
uv sync --frozen --no-default-groups
uv run --frozen --no-sync --env-file .env streamlit run app.py
```

Omit `--env-file .env` when the server already supplies environment values. For
notebooks/classification/decks add `--group analysis`; for the complete offline
suite install `uv sync --frozen --group analysis --group dev` and run
`uv run --frozen --no-sync pytest`. Analysis tools are optional for the dashboard.

`pyproject.toml` and `uv.lock` author the dependency graph. Generate the hashed
Docker manifest with `uv run --frozen --no-sync python tools/dependency_export.py`;
check drift with the same command plus `--check`. Docker installs that generated
`requirements.runtime.txt` with hash verification. Do not edit its pins directly.

Provide `JIRA_URL=https://amparex.atlassian.net`, `JIRA_USERNAME`, and
`JIRA_PASSWORD` (the account's API token) through the environment or an untracked
`.env` file injected by the launcher. Application modules do not read `.env` or
change process environment values. Cached analysis needs no Jira/model credentials;
missing credentials are reported only for a requested network operation. Never
commit credentials or ticket caches.

Inject `UI_PASSWORD` through the launcher or server environment to require a login
before ticket data or import controls are shown. The effective process environment
is the only application configuration source. Password changes revoke the current
session generation, including fragment actions. Without it, the dashboard remains accessible
without a password. Docker builds exclude local environment files, ticket caches,
and backups; provide credentials at runtime and use persistent storage for `data/`.
Only the logo and legacy category map from `data/` are bundled in the image.

`DATA_DIR` defaults to project-relative `data/`; a relative override is resolved
against the project directory, regardless of the shell's working directory. Static
assets stay under project-relative `assets/`. Explicit CLI path flags keep their
usual working-directory semantics. Jira tenant overrides are `JIRA_CLOUD_ID` and
`JIRA_WORKSPACE_ID`; their defaults preserve the historical tenant, and another
tenant cannot reuse an unproven legacy ticket/country cache. Versioned country
persistence for another tenant is delivered by cleanup phase 4.

## Euronet import

Euronet uses the existing Amparex custom-field IDs and `Fertig` completion logic.
It has no configured escalation targets. Generic issue links remain visible.
Reporting retains Berlin time and weekdays 08:00–18:00. Holidays are the German
public holidays for **Baden-Württemberg** (`subdiv="BW"`), set once in
`_load_service_desk_issues` and therefore shared by every desk, Euronet included.
Beyond the federal holidays this adds Heilige Drei Könige, Fronleichnam and
Allerheiligen; measured against the stored data it shifts business hours for
about 2% of tickets and moves 12 across a reporting band.
The current Erstlösequote view classifies same-calendar-day resolution.

Euronet uses the same requested creation-date window as Ipro and Amparex, with
no desk-specific minimum date. Older Euronet tickets are included in dashboard
refreshes and backfills when they fall within that window.

```sh
# Initial Euronet import; ends at the time the command runs
uv run --frozen --no-sync --env-file .env python backfill_jira.py --project SDEU --start 2025-01-01

# All three desks, past 365 days
uv run --frozen --no-sync --env-file .env python backfill_jira.py

# Any project combination and a shorter period
uv run --frozen --no-sync --env-file .env python backfill_jira.py --project SDAX --project SDEU --days 7

# Reuse a matching checkpoint and fetch the remaining time interval
uv run --frozen --no-sync --env-file .env python backfill_jira.py --project SDEU --start 2025-01-01 --resume
```

`--start` and `--days` are mutually exclusive. Checkpoints are per project and
record the effective window and Assets provenance. Old date-only checkpoints are
refetched; matching modern checkpoints are filtered, re-enriched if necessary,
and extended through the current import time. A valid zero-ticket result succeeds
without changing the cache. Partial project failures return a nonzero exit status
while successful project results are merged.

The CLI uses the shared import service. It automatically repairs unresolved
`Hauptkategorie` and `Unterkategorie` values, including older cached tickets outside
the selected date window. Category references are verified against Jira, shared
object lookups are deduplicated, and transient failures are retried up to three
times. A verified empty category is `Nicht zugeordnet`; inaccessible or unresolved
categories remain visible and make the command exit nonzero. `--resume` reuses
compatible raw snapshots; it does not freshly download older checkpointed issues.

If an existing Euronet checkpoint starts at the former September 2026 boundary,
run the first expanded backfill **without `--resume`**. A checkpoint covering
only recent tickets cannot establish coverage for an earlier requested start.
A new UI import fetches its selected window freshly. The explicit **Import
fortsetzen** action resumes a compatible interrupted or incomplete request.

An interrupted partial download is reused when `--resume` uses the same start.
Its saved tickets are kept; the older remainder is fetched, plus any tickets
created after the interrupted run's end (for example when the default end of
"now" has moved on). A partial download from a different start is discarded.

## Temporary backfill / import button

In the collapsed **Interaktiv → Daten aktualisieren** section, the Ursprung and
Länder update buttons sit alongside **Backfill / Datenimport**. Pick a creation
date range and one or more service desks, then use **Ausgewählte Tickets laden**.
The defaults are 1 January 2025 through today, with Ipro, Amparex and Euronet
all selected. Both selected days are included in full in Berlin time; today
ends at the import start time. A page left open past midnight moves an end date
of "today" to the new day; any other chosen end date is kept. An incomplete
date range, a start after the end, or an empty desk selection disables the
import button. If another session started an import after this page was drawn,
the click is rejected with a warning; start again once that import finishes.
The selection is frozen while the import runs and stays visible if it fails.
These import controls are independent of the sidebar company/date filters.
Expand the sidebar date range afterward to include this history in the charts;
both pickers reach back to 1970.

Both the sidebar refresh and the custom import use one shared background worker
and the same service as `backfill_jira.py`. Progress is polled every five seconds.
The worker merges and saves automatically, even if the browser is closed. Existing
ticket keys are updated, new tickets appended, and older/unrelated rows retained.
Rows changed during the fetch and rows with newer Jira timestamps are protected;
protected/skipped rows are reported so they are not mistaken for refreshed data.
Category repair applies narrow patches, preserving concurrent country/source
changes and labels that another writer has already resolved.

One operation lock prevents competing UI/CLI imports against the same data
folder. A short separate cache lock protects the final merge and atomic save;
Jira and Assets network requests do not hold it. Changed existing caches receive
one `data/jira_data.pkl.bak-sync-*` backup. Failed desks keep their previous data.
Remaining category/Assets failures produce an incomplete result, never a plain
success. Source and country maintenance retain their own guarded commit paths.

Every open dashboard reloads when it observes a changed cache revision, including
CLI changes. Request/status metadata lives in `data/import_status.json`; it does
not contain ticket text or a full dataframe and is not the concurrency lock.
A server restart interrupts the thread but retains disk checkpoints. The UI
reports an interrupted request and offers **Import fortsetzen**. This deployment
still assumes one application replica; Azure Files locking/atomic replacement
must be verified before adding a separate Azure import job.

The sidebar keeps its current scope: all three desks in its selected creation-date
window, regardless of the company display filter. Only the sidebar automatically
repairs missing Ursprung values across the cache and publishes the combined raw
`jira_issues.json` snapshot. Country resolution remains a separate action.

Removal: delete `temporary_history_pull.py`,
`tests/test_temporary_history_pull.py`, this README section, and the
`render_history_pull` import/calls in `app.py` (including the temporary
Interaktiv tabs immediately before empty-state `st.stop()` calls). The regular
refresh, CLI backfill, saved tickets, and analytics do not depend on this module.

## Normal Assets workspace

All active enrichment and country-export calls use the normal workspace configured
in `jira_loader.py`:

- Cloud: `242cf880-c51a-4277-9381-781d5ae181df`
- Workspace: `9926cb30-3f07-4fb2-9c83-aa4fc551c721`

Asset references must match this workspace. Inaccessible or mismatched objects
produce explicit errors and `Unbekannt` category labels; there is no sandbox
fallback. Fresh imports resolve category labels directly from normal Assets and
retain `assets_cloud_id`, `assets_workspace_id`, and `asset_errors` in
`data/jira_issues.json` (not in the dashboard's Raw Data table).
The static category map is only a compatibility fallback for unenriched legacy
inputs, not for any normal-workspace fetch.

To refresh category labels already stored in the local cache:

```sh
uv run --frozen --no-sync --env-file .env python asset_migration.py
```

This resolves the distinct category IDs originally copied from production issue
fields. It preserves ticket rows, customer/branch IDs, non-Assets fields, and date
coverage. Customer/branch columns in the legacy dataframe are
raw reference IDs, so they require no label migration. The command creates a
`data/jira_data.pkl.bak-assets-*` backup and refuses to overwrite a cache that
changed during its API reads. If any lookup fails, it exits without changing the
cache or creating a backup. Correct Assets permissions and rerun to recover
labels previously marked unknown across the entire cache, then reload Streamlit.

If category lookups return HTTP 403 while customer objects work, check the
category schema permissions for the account configured as `JIRA_USERNAME` in
the app's environment. In Assets, open the category schema's **Schema
configuration → Roles**, grant **Object schema users** access to that account
or its group, and check any object-type role restrictions. See
[Atlassian's schema role instructions](https://support.atlassian.com/assets/docs/add-users-or-groups-to-an-object-schema-role/).
Being able to read Jira tickets and their category object IDs does not by itself
grant permission to read the category objects. After granting access, run the
category migration above with dashboard refreshes idle; it avoids a full ticket
download and backs up the cache before saving successfully resolved labels.

## Country resolution (Länder tab)

A ticket's country escalates Ansprechpartner → Filiale → Zentrale; the first
asset carrying a `Land` attribute wins. Only asset references from the normal
workspace are used — an objectId from another workspace is discarded rather
than resolved, because the country cache is keyed by objectId alone and a
foreign id would otherwise borrow an unrelated site's country.

The cache lives in `data/asset_country.json` as `{object_id: country_or_null}`.
A dashboard refresh fills `Land` from that cache without calling the Assets
API, so refreshed tickets are charted immediately. Assets the cache has never
seen become **`Land noch nicht ermittelt`** — distinct from `Kein Land am
Asset`, which means the asset was fetched and genuinely carries no country.
The Länder tab reports how many tickets are in that state.

```sh
# Resolve every asset the cache does not know yet, then write Land back
uv run --frozen --no-sync --env-file .env python backfill_country.py --dry-run
uv run --frozen --no-sync --env-file .env python backfill_country.py

# Re-resolve ids cached as null by older versions, which stored a failed
# lookup the same way as "this asset has no Land" and never retried it
uv run --frozen --no-sync --env-file .env python backfill_country.py --retry-unknown
```

Writing over the input takes a `data/jira_data.pkl.bak-*` backup first.

The **🌍 Länder aktualisieren** button on the _Interaktiv_ tab runs the same
resolution against the whole stored cache — every ticket, regardless of the
selected period and company — and is how a hosted instance whose data differs
from the local cache brings its existing tickets up to date. It needs Assets
credentials, and a cold cache means one API call per unknown asset, so the
first run can take a while.

## Storage and validation

The dashboard reads `data/jira_data.pkl`. Imports use key-based upserts with
backups and atomic file replacement. The CLI and both UI import controls share
an operation lock; a competing import is rejected while one is active. Safe
merging preserves intervening source/country maintenance updates. To roll back
a migration, stop writers and restore its backup to `data/jira_data.pkl`.

A fully successful dashboard refresh writes all desks' fetched raw results once
to `data/jira_issues.json`. A project failure preserves the previous raw snapshot.
This JSON represents the fetched window, not the full historical pickle cache.
Its `data/jira_issues.meta.json` companion retains the originating run ID, request
window and desks, publication time, issue count, and SHA-256 digest even after a
later import replaces the shared status. The digest must match the raw file
before using that metadata: publication errors can leave an older sidecar.

```sh
uv run --frozen --no-sync --env-file .env python -m pytest -q
```

Tests cover company subsets and Streamlit rendering, import timezone boundaries,
checkpoint reuse, partial failures, Assets routing, legacy transformation fixtures,
Euronet escalation flags, upsert behavior, and failed cache writes. They use
synthetic records and replace external API calls; no live account is required.

See [the implementation plan](docs/euronet-service-desk-plan.md) and
[validation results](docs/euronet-validation.md) for the rollout record.
Ticket artifacts store schema version 2 in `DataFrame.attrs`. Ordinary reads
normalize unversioned/version 1 data in memory and preserve its bytes on disk.
Corrupt, malformed and unsupported future schemas report an explicit error.
Pickles remain trusted local artifacts; schema validation does not make
third-party pickle files safe to open.

Inspect a schema migration before applying it:

```sh
uv run --frozen --no-sync --env-file .env python cache_maintenance.py migrate --path data/jira_data.pkl
```

Review the printed versions, row count and revision. To apply, supply that exact
revision as `INODE:SIZE:MTIME_NS` (the three printed integers joined with colons):

```sh
uv run --frozen --no-sync --env-file .env python cache_maintenance.py migrate --path data/jira_data.pkl --apply --expected-revision 'INODE:SIZE:MTIME_NS'
```

Apply acquires the import-operation lock before the ticket transaction lock,
rechecks the source, creates an exact `.bak-schema-<UTC timestamp>` backup and
then atomically publishes v2. A changed source, active import or failed backup
prevents publication. Already-current data is a no-op. Exit codes are 0 for
success, 2 for invalid/stale/busy input and 3 for I/O failure. No Jira/model calls
occur. Use an explicit path under your configured `DATA_DIR` for mounted data.
Stop all writers before rollback and restore the paired application version and
untouched backup together; never run old/new schema writers on one directory.
