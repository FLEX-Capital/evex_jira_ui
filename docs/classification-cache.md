# Classification cache

The maintained classifier uses Azure OpenAI through an explicitly selected
deployment. Install the locked analysis group and inject the environment through
the launcher. `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, and
`AZURE_OPENAI_DEPLOYMENT_NAME` are required only when an explicit classification
invocation has valid pending work. `AZURE_OPENAI_API_VERSION` has the shared
settings default. Reading configuration or a cache and visiting the dashboard
never constructs a model client or performs model discovery.

## Reuse and returned results

The version 2 parquet stores independent identities:

- Ticket key and full SHA-256 of the rendered, truncated text.
- A configuration digest covering endpoint, deployment, API version, exact
  system prompt and taxonomy/schema, renderer and policy versions, all sent
  generation settings, and the text cap. Credentials are excluded.
- UTC completion time and result/error fields. Equal identity/time ties choose
  the larger SHA-256 of canonical sorted compact JSON for the result record.

The defaults retain a 6,000-character text prefix plus the truncation marker and
4,000 maximum completion tokens. The first fields are summary, category,
description, and comments. Parent/sub-bucket membership and emitted taxonomy
values are validated. Missing scalars and empty optional containers normalize
before rendering; malformed inputs do not trigger model requests.

Use [classifier_configuration](../ticket_classifier.py) to capture the current
settings. Bump `CLASSIFICATION_POLICY_VERSION` when a deployment alias is rebound
to another backing model. Changing policy marks matching work pending only on
the next explicit classification invocation. Rotating a credential alone does
not invalidate results. An explicit positional `model` and supplied configuration
must agree on the deployment.

Reusable Azure SDK clients contribute their effective endpoint, fixed deployment
route (if any), and API query version before cache selection. An explicit
configuration must agree with that route. Non-SDK fake clients retain the explicit
testing seam. Configurations captured under another prompt, taxonomy, schema,
renderer, or policy implementation are rejected; capture a fresh configuration
after code changes. Changing digest labels alone cannot request an older
implementation. Frozen nested generation values are copied to ordinary JSON
containers only at the request boundary.

`classify_tickets` returns at most one matching result or safe input-error row per
requested key. Historical configurations/texts and unrelated keys remain stored
and are excluded from that return value. Later duplicate-key inputs take
precedence, including a later invalid input. Valid-key input errors are returned
for presentation and are never cached. Missing-key errors appear only in
`frame.attrs["classification_errors"]`, with zero-based input row positions and
safe codes. Filter successful results with `error.isna()` before aggregation.

Matching cached, empty, and invalid-only batches need no client. Failed matching
identities are retried by the next explicit invocation. `force`, `limit`,
`save_every`, and explicit fake/reusable clients remain supported. Periodic
flushes merge only prepared records against the latest disk state, under its
owning lock. Model calls occur outside that lock. An interrupted invocation may
need an explicit retry for results that were not flushed.

## Legacy metadata migration

Unversioned rows remain `legacy_unverified`, including preexisting identity/time
columns and duplicate historical records. They are retained but never reused as
current results. Metadata migration does not invent fingerprints or classify
anything. A later explicit classification invocation can therefore require paid
work for legacy tickets.

Inspection is read-only and creates no lock, backup, or temporary artifact:

```sh
uv run --frozen --group analysis --env-file .env python classification_cache.py migrate \
  --path data/sdax_classifications.parquet
```

Apply requires the exact revision printed by inspection. It rechecks under the
owning lock, backs up exact original bytes, then atomically publishes metadata:

```sh
uv run --frozen --group analysis --env-file .env python classification_cache.py migrate \
  --path data/sdax_classifications.parquet --apply \
  --expected-revision INODE:SIZE:MTIME_NS
```

Use the actual configured artifact path when `DATA_DIR` differs. Exit codes are
0 inspected/applied/already current, 2 invalid or conflicting input, and 3 I/O
failure. Future versions and corrupt artifacts fail explicitly. Path aliases
resolve once and publish to the same canonical artifact.

Keep old and new writers out of the same artifact during transition. Roll back
the application and its paired untouched backup together. Local atomicity and
failure tests do not certify Azure Files durability; that acceptance remains a
separate staging check.
