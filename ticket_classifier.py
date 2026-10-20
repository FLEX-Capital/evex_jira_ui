"""Classify Amparex (SDAX) service-desk tickets by how they were resolved.

The question this answers: which customer-support requests could an AI handle,
and which genuinely need a human or another team? Every ticket is sent to Claude
with its summary, description and comments, and comes back with

    resolution_bucket   how the ticket was (or would be) resolved
    sub_bucket          a finer split inside that bucket
    intent_label        a free-text canonical intent, 2-5 German words
    ai_mode             how much of it AI could take over
    confidence          0..1
    reasoning           one sentence, so a human can spot-check the label

``intent_label`` is deliberately unconstrained: the smaller recurring buckets are
discovered by aggregating it afterwards rather than guessed up front.

Results are cached per ticket key in a parquet file, so re-running after a Jira
refresh only pays for tickets that are new. Ticket text is hashed into the cache;
a ticket whose text changed is reclassified, one that merely got touched is not.

Backend: Azure OpenAI. Configure via .env::

    AZURE_OPENAI_API_KEY=...
    AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
    AZURE_OPENAI_DEPLOYMENT_NAME=<your deployment name>   # not the model name
    AZURE_OPENAI_API_VERSION=2024-10-21              # optional, this is the default

``model=`` is the *deployment* name on Azure, not a model id. The deployment must
be backed by a model that supports structured outputs (gpt-4o-2024-08-06 or
later, including gpt-4o-mini); older deployments reject ``response_format`` with
a json_schema and every ticket will come back as a bad_request error.
"""

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import openai
import pandas as pd
from openai import AzureOpenAI
from pydantic import BaseModel, Field

import settings
from settings import load_settings

CACHE_PATH = "data/sdax_classifications.parquet"

# 2024-10-21 is the oldest GA api-version with structured outputs. Override via
# environment if your resource is pinned to a newer one.
DEFAULT_API_VERSION = settings.DEFAULT_API_VERSION


MAX_COMPLETION_TOKENS = 4000


def resolve_deployment() -> str:
    """Return the deployment alias resolved from the current environment."""
    return load_settings().azure_deployment or ""


RESOLUTION_BUCKETS = (
    "question_answer",
    "support_todo",
    "team_handoff",
    "unclear",
)

SUB_BUCKETS = (
    # question_answer
    "how_to",
    "where_to_find",
    "policy_or_process_clarification",
    # support_todo
    "data_correction",
    "document_or_invoice_resend",
    "configuration_change",
    "permissions_or_access",
    "manual_workaround",
    # team_handoff
    "bug",
    "feature_request",
    "database_fix",
    "hardware_or_infrastructure",
    "external_vendor",
    # unclear
    "insufficient_information",
)

AI_MODES = (
    "fully_automatable",
    "ai_drafts_human_sends",
    "ai_assisted_triage",
    "human_only",
)


class TicketClassification(BaseModel):
    """One classified ticket. Field order is the order Claude reasons in."""

    reasoning: str = Field(
        description="One sentence in English explaining the classification."
    )
    resolution_bucket: str = Field(
        description=f"One of: {', '.join(RESOLUTION_BUCKETS)}"
    )
    sub_bucket: str = Field(description=f"One of: {', '.join(SUB_BUCKETS)}")
    intent_label: str = Field(
        description=(
            "The recurring request type in 2-5 German words, lowercase, "
            "generic enough that similar tickets get the identical label."
        )
    )
    ai_mode: str = Field(description=f"One of: {', '.join(AI_MODES)}")
    confidence: float = Field(description="0.0 to 1.0.", ge=0.0, le=1.0)


SYSTEM_PROMPT = """\
You classify tickets from the Amparex customer-support service desk (Jira project
SDAX). Amparex is ERP software for German hearing-aid acousticians and opticians:
it covers patient/customer records, hearing-aid fitting, warehouse and stock,
cash register and TSE, invoicing to statutory health insurers (Kostenträger),
appointment scheduling, document templates and store networking.

The tickets are written in German by support agents and customers, often in
telephone-note shorthand. Text is frequently terse or incomplete. Judge what the
ticket is really asking for, not how polished the writing is.

Return exactly one classification per ticket.

## resolution_bucket - how the request gets resolved

- question_answer: the customer asked something answerable from product
  knowledge or documentation. Nothing in the system has to be changed. Includes
  "how do I do X", "where do I find Y", "is Z allowed / how does the process
  work". A ticket resolved by linking a documentation article belongs here.
- support_todo: a support rep has to *do* something concrete, but it is routine
  work inside the normal application - correct a record, cancel and reissue an
  invoice, reset or change a setting, grant a permission, resend a document,
  apply a known workaround. No engineering involvement.
- team_handoff: resolving it requires someone outside first-level support - a
  developer (software defect, missing capability), a database or data-migration
  fix that cannot be done through the UI, hardware/infrastructure work, or an
  external vendor (printer, EC terminal, TSE provider, insurer).
- unclear: the text does not contain enough information to tell. Use this
  rather than guessing. Do not use it merely because the text is short.

## sub_bucket - must belong to the chosen resolution_bucket

- question_answer: how_to, where_to_find, policy_or_process_clarification
- support_todo: data_correction, document_or_invoice_resend,
  configuration_change, permissions_or_access, manual_workaround
- team_handoff: bug, feature_request, database_fix,
  hardware_or_infrastructure, external_vendor
- unclear: insufficient_information

## intent_label - the emergent taxonomy

2-5 lowercase German words naming the recurring request type, e.g.
"rechnung stornieren", "passwort zuruecksetzen", "kostenvoranschlag abgelehnt",
"drucker druckt nicht". Two tickets asking the same thing must get the identical
string, so stay generic: no customer names, ticket numbers, dates or amounts.
Use plain ASCII (write ue/oe/ae/ss for umlauts and sharp s).

## ai_mode - how much an AI assistant could take over

Judge the request itself, independently of resolution_bucket.

- fully_automatable: an AI with product documentation and read access could
  resolve it end to end without a human reading the answer first.
- ai_drafts_human_sends: an AI could produce the complete answer or carry out
  the action, but a human should approve it - because money, patient data,
  insurer billing or an irreversible change is involved.
- ai_assisted_triage: AI cannot resolve it, but can usefully prepare it -
  summarise, gather the missing facts, route it to the right team.
- human_only: needs human judgement, a physical action, or a conversation.
"""


def build_client(deployment: str | None = None) -> AzureOpenAI:
    """Construct the requested deployment client from one settings snapshot."""
    connection = load_settings().require_classifier(deployment)
    return AzureOpenAI(
        api_key=connection.api_key,
        azure_endpoint=connection.endpoint,
        api_version=connection.api_version,
        max_retries=5,
    )


def _hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build_ticket_text(row, max_chars=6000):
    """Render one ticket into the prompt text.

    Comments carry the resolution and are the most informative field, but they
    are also the longest and are absent on most tickets. Summary and category
    always come first so a truncated ticket still shows what it was about.
    """
    parts = [f"Summary: {str(row.get('summary') or '').strip()}"]

    haupt = str(row.get("Hauptkategorie") or "").strip()
    unter = str(row.get("Unterkategorie") or "").strip()
    if haupt and haupt.lower() not in ("nan", "none"):
        category = haupt
        if unter and unter.upper() not in ("NA", "NAN", "NONE"):
            category += f" / {unter}"
        parts.append(f"Category: {category}")

    description = str(row.get("description") or "").strip()
    if description and description.lower() not in ("nan", "none"):
        parts.append(f"Description: {description}")

    comments = str(row.get("comments") or "").strip()
    if comments and comments.lower() not in ("nan", "none", "[]"):
        parts.append(f"Comments:\n{comments}")

    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[...truncated]"
    return text


def load_cache(cache_path=None):
    """Return the cached classifications, or an empty frame with the schema."""
    columns = [
        "key",
        "text_hash",
        "resolution_bucket",
        "sub_bucket",
        "intent_label",
        "ai_mode",
        "confidence",
        "reasoning",
        "model",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "error",
    ]
    cache_path = (
        load_settings().paths.classifications
        if cache_path is None
        else Path(cache_path)
    )
    if os.path.exists(cache_path):
        cached = pd.read_parquet(cache_path)
        for column in columns:
            if column not in cached.columns:
                cached[column] = pd.NA
        return cached[columns]
    return pd.DataFrame(columns=columns)


def save_cache(frame, cache_path=None):
    cache_path = (
        load_settings().paths.classifications
        if cache_path is None
        else Path(cache_path)
    )
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    temporary = f"{cache_path}.tmp"
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, cache_path)


def classify_one(client, key, text, model=None):
    """Classify a single ticket. Returns a result dict; never raises."""
    model = model or resolve_deployment()
    try:
        response = client.chat.completions.parse(
            model=model,
            # Reasoning deployments (gpt-5.x) reject any temperature but the
            # default and spend part of this budget on hidden reasoning tokens,
            # so the ceiling is well above what the JSON answer alone needs.
            # Running out shows up as a LengthFinishReasonError, not silence.
            max_completion_tokens=MAX_COMPLETION_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Ticket {key}\n\n{text}"},
            ],
            response_format=TicketClassification,
        )
        message = response.choices[0].message
        # Azure's content filter can decline a ticket; that is a real outcome for
        # support text (medical context, personal data), not a transport error,
        # so record it instead of retrying it forever.
        if message.refusal:
            raise ValueError(f"refusal: {message.refusal}")
        parsed = message.parsed
        if parsed is None:
            raise ValueError("no parsed output returned")
        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        return {
            "key": key,
            "text_hash": _hash_text(text),
            "resolution_bucket": parsed.resolution_bucket,
            "sub_bucket": parsed.sub_bucket,
            "intent_label": parsed.intent_label,
            "ai_mode": parsed.ai_mode,
            "confidence": parsed.confidence,
            "reasoning": parsed.reasoning,
            "model": model,
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            # Reasoning deployments bill these as output; they dominate the cost
            # on gpt-5.x, so the estimate must come from measurement not a guess.
            "reasoning_tokens": getattr(details, "reasoning_tokens", None),
            "error": None,
        }
    # Most-specific first: a bad request or a missing deployment is a bug in the
    # configuration and must not be silently retried by the caller as if it were
    # load. On Azure a wrong deployment name surfaces as NotFoundError.
    except openai.LengthFinishReasonError as exc:
        error = f"truncated: {exc}"
    except openai.ContentFilterFinishReasonError as exc:
        error = f"content_filter: {exc}"
    except openai.BadRequestError as exc:
        error = f"bad_request: {exc}"
    except openai.NotFoundError as exc:
        error = f"not_found (check AZURE_OPENAI_DEPLOYMENT_NAME): {exc}"
    except openai.RateLimitError as exc:
        error = f"rate_limited: {exc}"
    except openai.APIStatusError as exc:
        error = f"http_{exc.status_code}: {exc}"
    except openai.APIConnectionError as exc:
        error = f"connection: {exc}"
    except Exception as exc:  # noqa: BLE001 - one bad ticket must not kill the run
        error = f"{type(exc).__name__}: {exc}"
    return {
        "key": key,
        "text_hash": _hash_text(text),
        "resolution_bucket": None,
        "sub_bucket": None,
        "intent_label": None,
        "ai_mode": None,
        "confidence": None,
        "reasoning": None,
        "model": model,
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "error": error,
    }


def classify_tickets(
    df,
    cache_path=None,
    model=None,
    max_workers=8,
    limit=None,
    force=False,
    save_every=100,
    progress=True,
    client=None,
):
    """Classify every ticket in ``df`` that is not already cached.

    ``df`` needs the columns build_ticket_text() reads, plus ``key``. Returns
    the full cache frame (cached + newly classified). Tickets that previously
    errored are retried; tickets whose text is unchanged are not re-sent.

    ``client`` is built from the environment when omitted; pass one to reuse a
    client across calls or to substitute a double in tests.
    """
    cache_path = (
        load_settings().paths.classifications
        if cache_path is None
        else Path(cache_path)
    )
    model = model or resolve_deployment()
    cache = load_cache(cache_path)
    texts = {row["key"]: build_ticket_text(row) for _, row in df.iterrows()}

    if force:
        done = {}
    else:
        successful = cache[cache["error"].isna() & cache["resolution_bucket"].notna()]
        done = dict(zip(successful["key"], successful["text_hash"], strict=False))

    pending = [
        (key, text) for key, text in texts.items() if done.get(key) != _hash_text(text)
    ]
    if limit is not None:
        pending = pending[:limit]

    if not pending:
        print(f"Nothing to classify - {len(cache)} tickets already cached.")
        return cache

    estimated = sum(len(text) for _, text in pending) / 4
    print(
        f"Classifying {len(pending)} tickets with {model} "
        f"(~{estimated / 1000:.0f}k input tokens, {max_workers} workers)."
    )

    if client is None:
        client = build_client(model)
    results = []
    lock = threading.Lock()
    bar = None
    if progress:
        try:
            from tqdm.auto import tqdm

            bar = tqdm(total=len(pending), unit="ticket")
        except ImportError:
            bar = None

    def flush():
        """Merge results into the cache on disk so a crash loses at most save_every."""
        fresh = pd.DataFrame(results)
        # An empty cache is all-NA columns; concatenating it would both raise a
        # pandas FutureWarning and widen the fresh dtypes to object.
        frames = [frame for frame in (cache, fresh) if not frame.empty]
        merged = pd.concat(frames, ignore_index=True) if frames else fresh
        # keep="last" lets a fresh result supersede a cached failure; the reset
        # matters because callers index the returned frame positionally.
        merged = merged.drop_duplicates(subset="key", keep="last").reset_index(
            drop=True
        )
        save_cache(merged, cache_path)
        return merged

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(classify_one, client, key, text, model): key
            for key, text in pending
        }
        for future in as_completed(futures):
            result = future.result()
            with lock:
                results.append(result)
                if bar is not None:
                    bar.update(1)
                if len(results) % save_every == 0:
                    flush()

    if bar is not None:
        bar.close()
    merged = flush()

    failed = sum(1 for result in results if result["error"])
    print(f"Done: {len(results) - failed} classified, {failed} failed.")

    ok = [result for result in results if not result["error"]]
    if ok:
        totals = {
            name: sum(result[name] or 0 for result in ok)
            for name in ("input_tokens", "output_tokens", "reasoning_tokens")
        }
        print(
            f"Tokens/ticket (mean over {len(ok)}): "
            f"in {totals['input_tokens'] / len(ok):.0f}, "
            f"out {totals['output_tokens'] / len(ok):.0f} "
            f"(davon reasoning {totals['reasoning_tokens'] / len(ok):.0f})"
        )
    if failed:
        reasons = pd.Series(
            [r["error"].split(":")[0] for r in results if r["error"]]
        ).value_counts()
        print("Failure reasons:")
        print(reasons.to_string())
    return merged
