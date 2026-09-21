# Jira Analytics Dashboard

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

Use Python 3.12 or newer and the pinned runtime dependencies:

```sh
uv venv
uv pip install -r requirements.runtime.txt
uv run --no-sync streamlit run app.py
```

Provide `JIRA_URL=https://amparex.atlassian.net`, `JIRA_USERNAME`, and
`JIRA_PASSWORD` (the account's API token) through the environment or an untracked
`.env` file. Cached analysis does not require a live Jira connection. The existing
code loads `.env` with override enabled. Never commit credentials or ticket caches.

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
uv run --no-sync python backfill_jira.py --project SDEU --start 2025-01-01

# All three desks, past 365 days
uv run --no-sync python backfill_jira.py

# Any project combination and a shorter period
uv run --no-sync python backfill_jira.py --project SDAX --project SDEU --days 7

# Reuse a matching checkpoint and fetch the remaining time interval
uv run --no-sync python backfill_jira.py --project SDEU --start 2025-01-01 --resume
```

`--start` and `--days` are mutually exclusive. Checkpoints are per project and
record the effective window and Assets provenance. Old date-only checkpoints are
refetched; matching modern checkpoints are filtered, re-enriched if necessary,
and extended through the current import time. A valid zero-ticket result succeeds
without changing the cache. Partial project failures return a nonzero exit status
while successful project results are merged.

If an existing Euronet checkpoint starts at the former September 2026 boundary,
run the first expanded backfill **without `--resume`**. A checkpoint covering
only recent tickets cannot establish coverage for an earlier requested start.
The temporary full-history button below always fetches its entire window.

## Temporary full-history button

In the collapsed **Interaktiv → Daten aktualisieren** section, the Ursprung and
Länder update buttons sit alongside the temporary import. Use **Alle Tickets seit
01.01.2025 laden** to download all three desks through the import start time.
All three desks begin at 1 January 2025, 00:00 Berlin.
Dashboard company/date filters do not limit the pull.
Expand the sidebar date range afterward to include this history in the charts.

The background thread survives page reruns and browser reconnects. Progress is
polled every five seconds while the page is open. Return to the dashboard to
merge a finished download into the latest cache: existing ticket keys are
updated, new tickets appended, and older/unrelated rows retained. Rows changed
in the cache while the import was running are retained in full, as are cached
rows with newer Jira update timestamps. This preserves intervening ticket,
Ursprung, category, and country updates; rerun the import later to refresh any
rows retained because of concurrent changes. A
`data/jira_data.pkl.bak-history-*` backup is made before saving. Failed desks
retain their cached data and are reported separately; Assets lookup failures
remain visible as warnings, including unresolved category labels.

Cache commits use a shared file lock across dashboard sessions and CLI writers.
Maintenance actions based on a stale cache snapshot stop with a retry message
instead of overwriting another writer's result. Every open dashboard session
reloads once when it observes a completed import, including sessions that did
not start or apply the import.

This temporary worker runs once per server process. A server restart discards
an unfinished/unapplied download; start it again afterward. It is intended for
the existing single-process deployment, not multiple application replicas.
Use the CLI backfill above if resumable disk checkpoints are required.

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
uv run --no-sync python asset_migration.py
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
uv run --no-sync python backfill_country.py --dry-run
uv run --no-sync python backfill_country.py

# Re-resolve ids cached as null by older versions, which stored a failed
# lookup the same way as "this asset has no Land" and never retried it
uv run --no-sync python backfill_country.py --retry-unknown
```

Writing over the input takes a `data/jira_data.pkl.bak-*` backup first.

The **🌍 Länder aktualisieren** button on the _Interaktiv_ tab runs the same
resolution against the whole stored cache — every ticket, regardless of the
selected period and company — and is how a hosted instance whose data differs
from the local cache brings its existing tickets up to date. It needs Assets
credentials, and a cold cache means one API call per unknown asset, so the
first run can take a while.

## Storage and validation

The dashboard reads `data/jira_data.pkl`. Backfills use key-based upserts with
backups; saves use atomic file replacement. Avoid refreshing the dashboard while
a CLI import or cache migration is writing. To roll back a migration, stop writers
and restore its backup to `data/jira_data.pkl`.

A fully successful dashboard refresh writes all desks' fetched raw results once
to `data/jira_issues.json`. A project failure preserves the previous raw snapshot.
This JSON represents the fetched window, not the full historical pickle cache.

```sh
uv run --no-sync python -m unittest discover -s tests
```

Tests cover company subsets and Streamlit rendering, import timezone boundaries,
checkpoint reuse, partial failures, Assets routing, legacy transformation fixtures,
Euronet escalation flags, upsert behavior, and failed cache writes. They use
synthetic records and replace external API calls; no live account is required.

See [the implementation plan](docs/euronet-service-desk-plan.md) and
[validation results](docs/euronet-validation.md) for the rollout record.
