# Supported analysis

The single maintained notebook is [amparex_ai_assist_analysis.ipynb](../amparex_ai_assist_analysis.ipynb). Earlier exploration is preserved in the [unsupported archive](../analysis/archive/README.md). The [board provenance](../analysis/historical/board-provenance.json) describes the frozen historical deck; new analysis never rewrites its literals.

Install the locked analysis group with `uv sync --frozen --group analysis --group dev`. Supply environment variables through the shell or `uv run --env-file .env`; application modules do not parse .env files. Start Jupyter from the repository, or provide the absolute `REPO_ROOT` parameter before executing from another directory. `DATA_DIR` selects the mutable cache root independently of cwd. No credentials are needed to inspect a trusted local cache.

Set `PROJECT`, `START_DATE` and inclusive `END_DATE` (Europe/Berlin); the default end is Berlin today. Selection happens after shared cache normalization. Modern unknown/foreign workspace categories remain unknown; only shared legacy compatibility may resolve old IDs.

`REFRESH=False` and `RUN_CLASSIFICATION=False` are defaults. Refresh is a separate explicit Jira operation. Classification opt-in can incur model charges and requires the environment configuration in the [main setup guide](../README.md). Cache-only mode selects matching current input/config identities and never starts a model client. Preview is cache-only; the full step is the single paid-work step. Classification configuration fingerprints the current prompt, taxonomy, renderer and policy; do not relabel a fingerprint to reuse old results.

The manifest reports selected/excluded counts, the coherent local cache-read timestamp, source-artifact SHA-256, selected-input SHA-256 and classifier identity. The read timestamp does not establish Jira freshness. Concurrent replacement between load and hashing aborts safely; rerun with a stable cache. Key joins reject duplicates. Missing current results are counted explicitly rather than presented as classified tickets.

`OUTPUT_DIR=None` writes no files. Choose an output directory explicitly to export only the manifest and aggregate category/intent CSVs. Do not save private ticket payloads into notebook execution outputs. Run `python tools/notebook_outputs.py --check` before committing any notebook.
