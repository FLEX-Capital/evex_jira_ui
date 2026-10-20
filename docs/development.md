# Development and supported architecture

Use Python 3.12 and `uv`. The locked graph in [pyproject.toml](../pyproject.toml)
and [uv.lock](../uv.lock) supplies three installation scopes. The dashboard and
maintenance commands need only the default runtime:

```sh
uv sync --frozen --no-default-groups
uv run --frozen --no-sync --env-file .env streamlit run app.py
```

The launcher injects the untracked `.env`; omit `--env-file .env` when values are
already in the server environment. Application modules read captured environment
settings and never parse `.env`. Jira credentials are required only for explicit
network operations. `UI_PASSWORD` enables the gate before ticket reads; changing
it revokes previous sessions. An unset password allows public access.

For the [supported analysis workflow](analysis.md), add `--group analysis` to
`uv sync --frozen`. For all development checks:

```sh
uv sync --frozen --group analysis --group dev
uv run --frozen --no-sync pytest
uv run --frozen --no-sync python tools/check_python.py --all --types
uv run --frozen --no-sync python tools/dependency_export.py --check
uv run --frozen --no-sync python tools/notebook_outputs.py --check
uv run --frozen --no-sync python tools/check_docs.py --tracked
```

Pytest is the supported runner, including fixtures, parametrization, notebook
execution and offline guards. Tests use synthetic records and injected fake
clients. They need no live Jira or model credentials. Runtime installation and
container integration tests require their explicit opt-in flags and are separate
from the ordinary offline suite. Notebook execution starts local Jupyter kernel
ports. Analysis and deck coverage is reported separately from application coverage.

Generate `requirements.runtime.txt` with
`uv run --frozen --no-sync python tools/dependency_export.py`; its hash-checked
pins are generated from the lock, never independently authored. CI checks byte
parity. Ruff is the sole Python formatter/import authority. Run
`uv run --frozen --no-sync ruff format .` before the native quality check. Git
hooks validate isolated index bytes with Ruff and one strict mypy batch; Trunk
also retains its non-Python checks. Historical archive source is excluded from
lint/type checking; all notebooks, including the archive, must have no saved
outputs or execution counts. Tests and the frozen historical deck have explicit
separate typing scope; maintained analysis helpers remain fully checked.

## Configuration and storage

[settings.py](../settings.py) captures one immutable settings snapshot per
operation. `DATA_DIR` defaults to project-relative `data/`; relative overrides
resolve against the project directory, independent of the working directory.
Static `assets/` are always project-relative and stay outside mutable data.
Explicit CLI path flags retain their ordinary working-directory semantics.
See the [main setup guide](../README.md#run-locally) for credential names and
[storage and validation](../README.md#storage-and-validation) for full procedures.

Ticket schema 2 normalizes legacy inputs in memory; reads do not migrate disk
bytes. Country schema 2 keys results by cloud, workspace and object identity,
with separate successful-empty and failed outcomes. Unverified legacy country
entries never prove another tenant's identity. Classification schema 2 retains
input/config identities and history under an atomic latest-disk merge; see the
[classification cache contract](classification-cache.md). Model calls require
explicit opt-in. These are trusted local artifacts, including pickle files.

Inspect before explicitly applying any mutation:

```sh
uv run --frozen --no-sync --env-file .env python cache_maintenance.py migrate --path data/jira_data.pkl
uv run --frozen --no-sync --env-file .env python cache_maintenance.py cleanup --data-dir data
uv run --frozen --no-sync --env-file .env python country_cache.py migrate --path data/asset_country.json
```

Migration apply requires the reviewed revision; retention apply requires the
reviewed plan digest. Apply creates backups and revalidates concurrent state.
Pruning is irreversible. Stop writers and restore the paired application version
and exact backup together for rollback. See the main guide before adding apply
flags. Country refresh is explicit, for example
`uv run --frozen --no-sync --env-file .env python backfill_country.py --force-refresh`.
Ordinary chart rendering never triggers enrichment or paid classification.

## Shared boundaries

[service_desks.py](../service_desks.py), [reporting_policy.py](../reporting_policy.py)
and [issue_links.py](../issue_links.py) own desk identities, Berlin calendar/field
policy and typed link selection. [file_io.py](../file_io.py) owns short locks,
coherent file revisions and atomic replacement. Network calls run outside cache
locks. [desk_sync.py](../desk_sync.py) coordinates CLI/sidebar/custom imports;
[sync_checkpoints.py](../sync_checkpoints.py) owns resumable download artifacts.
Safe aggregate diagnostics carry operation IDs and codes without response bodies
or ticket text. [dashboard_views.py](../dashboard_views.py) and
[chart_data.py](../chart_data.py) separate rendering and deterministic aggregation
from the Streamlit entry point. Authentication is rechecked before maintenance
publication. See the [cleanup roadmap](repository-cleanup-roadmap-2026-10-19.md)
for the issue ownership and acceptance sequence.

Local planning and review working files under `docs/superpowers/` are ignored
and are optional. This tracked guide and the roadmap contain the supported
architecture/setup contract; a fresh clone needs none of those local files.
Production ingress, widgets, Azure Files locking/atomic replacement, restart and
durable storage acceptance require separate staging evidence at one replica.
