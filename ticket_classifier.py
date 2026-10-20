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
import threading
from collections.abc import Mapping, Sized
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Literal, Self, get_args
from uuid import uuid4

import openai
import pandas as pd
from openai import AzureOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

import settings
from operation_errors import safe_diagnostic
from settings import load_settings

CACHE_PATH = "data/sdax_classifications.parquet"

# 2024-10-21 is the oldest GA api-version with structured outputs. Override via
# environment if your resource is pinned to a newer one.
DEFAULT_API_VERSION = settings.DEFAULT_API_VERSION


MAX_COMPLETION_TOKENS = 4000


def resolve_deployment() -> str:
    """Return the deployment alias resolved from the current environment."""
    return load_settings().azure_deployment or ""


ResolutionBucket = Literal["question_answer", "support_todo", "team_handoff", "unclear"]
QuestionAnswerSubBucket = Literal[
    "how_to", "where_to_find", "policy_or_process_clarification"
]
SupportTodoSubBucket = Literal[
    "data_correction",
    "document_or_invoice_resend",
    "configuration_change",
    "permissions_or_access",
    "manual_workaround",
]
TeamHandoffSubBucket = Literal[
    "bug",
    "feature_request",
    "database_fix",
    "hardware_or_infrastructure",
    "external_vendor",
]
UnclearSubBucket = Literal["insufficient_information"]
SubBucket = Literal[
    QuestionAnswerSubBucket,
    SupportTodoSubBucket,
    TeamHandoffSubBucket,
    UnclearSubBucket,
]
AIMode = Literal[
    "fully_automatable", "ai_drafts_human_sends", "ai_assisted_triage", "human_only"
]
RESOLUTION_BUCKETS = get_args(ResolutionBucket)
SUB_BUCKETS = get_args(SubBucket)
AI_MODES = get_args(AIMode)
_SUB_BUCKETS_BY_PARENT = {
    "question_answer": get_args(QuestionAnswerSubBucket),
    "support_todo": get_args(SupportTodoSubBucket),
    "team_handoff": get_args(TeamHandoffSubBucket),
    "unclear": get_args(UnclearSubBucket),
}


class TicketClassification(BaseModel):
    """One classified ticket. Field order is the order Claude reasons in."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        description="One sentence in English explaining the classification."
    )
    resolution_bucket: ResolutionBucket = Field(
        description=f"One of: {', '.join(RESOLUTION_BUCKETS)}"
    )
    sub_bucket: SubBucket = Field(description=f"One of: {', '.join(SUB_BUCKETS)}")
    intent_label: str = Field(
        description=(
            "The recurring request type in 2-5 German words, lowercase, "
            "generic enough that similar tickets get the identical label."
        )
    )
    ai_mode: AIMode = Field(description=f"One of: {', '.join(AI_MODES)}")
    confidence: float = Field(description="0.0 to 1.0.", ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_membership(self) -> Self:
        """Require the sub-bucket to belong to its selected parent."""
        if self.sub_bucket not in _SUB_BUCKETS_BY_PARENT[self.resolution_bucket]:
            raise ValueError("invalid_taxonomy_pair")
        return self


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


class TicketInputError(ValueError):
    """A safe row validation cause; source contents are never interpolated."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def scalar_text(value: object) -> str:
    """Normalize missing scalars/empty containers; reject nonempty containers.

    Args:
        value: One optional ticket cell.

    Returns:
        Trimmed scalar text, or empty text for a missing value.

    Raises:
        TicketInputError: A nonempty unsupported container is not ticket text.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sized) and len(value) == 0:
        return ""
    if not pd.api.types.is_scalar(value) or isinstance(
        value, (bytes, bytearray, memoryview)
    ):
        raise TicketInputError("invalid_text")
    if value is None or bool(pd.isna(value)):
        return ""
    return str(value).strip()


def _ticket_fields(row: Mapping[str, Any]) -> dict[str, str]:
    fields = {
        name: scalar_text(row.get(name))
        for name in (
            "summary",
            "Hauptkategorie",
            "Unterkategorie",
            "description",
            "comments",
        )
    }
    for name in ("Hauptkategorie", "description", "comments"):
        if fields[name].lower() in ("nan", "none") or (
            name == "comments" and fields[name] == "[]"
        ):
            fields[name] = ""
    if fields["Unterkategorie"].upper() in ("NA", "NAN", "NONE"):
        fields["Unterkategorie"] = ""
    return fields


def _render_ticket_text(fields: Mapping[str, str], max_chars: int) -> str:
    if type(max_chars) is not int or max_chars <= 0:
        raise TicketInputError("invalid_text_limit")
    parts = [f"Summary: {fields['summary']}"]
    if fields["Hauptkategorie"]:
        category = fields["Hauptkategorie"]
        if fields["Unterkategorie"]:
            category += f" / {fields['Unterkategorie']}"
        parts.append(f"Category: {category}")
    if fields["description"]:
        parts.append(f"Description: {fields['description']}")
    if fields["comments"]:
        parts.append(f"Comments:\n{fields['comments']}")
    text = "\n\n".join(parts)
    return text[:max_chars] + "\n[...truncated]" if len(text) > max_chars else text


def build_ticket_text(row: Mapping[str, Any], max_chars: int = 6000) -> str:
    """Render summary/category/description/comments with the existing cap and suffix.

    Args:
        row: One ticket's optional text fields; no key is needed for rendering.
        max_chars: Prefix length before the existing truncation marker.

    Returns:
        Deterministic prompt text, with summary first.
    """
    return _render_ticket_text(_ticket_fields(row), max_chars)


def validate_ticket_input(
    row: Mapping[str, Any], *, max_chars: int = 6000
) -> tuple[str, str]:
    """Validate a ticket identity and substantive text before scheduling model work.

    Args:
        row: One candidate ticket record.
        max_chars: Rendered-text prefix cap.

    Returns:
        The trimmed key and deterministic rendered text.

    Raises:
        TicketInputError: Invalid key, unsupported cell, or entirely empty ticket text.
    """
    key = row.get("key")
    if not isinstance(key, str) or not key.strip():
        raise TicketInputError("invalid_key")
    fields = _ticket_fields(row)
    if not any(fields[name] for name in ("summary", "description", "comments")):
        raise TicketInputError("empty_text")
    return key.strip(), _render_ticket_text(fields, max_chars)


def load_cache(cache_path: str | Path | None = None) -> pd.DataFrame:
    """Read full versioned classification history; selection belongs to the caller."""
    from classification_cache import load_classifications

    path = (
        load_settings().paths.classifications
        if cache_path is None
        else Path(cache_path)
    )
    return load_classifications(path)


def save_cache(
    frame: pd.DataFrame, cache_path: str | Path | None = None
) -> pd.DataFrame:
    """Merge prepared results with the latest disk identities under its own transaction."""
    from classification_cache import merge_classifications

    path = (
        load_settings().paths.classifications
        if cache_path is None
        else Path(cache_path)
    )
    return merge_classifications(path, frame)


class ClassificationOutputError(ValueError):
    """A bounded refusal/empty-output cause, independent of provider payload text."""

    def __init__(self, code: Literal["refusal", "invalid_output"]) -> None:
        self.code = code
        super().__init__(code)


def classify_one(
    client: Any, key: str, text: str, model: str | None = None
) -> dict[str, Any]:
    """Classify one ticket, retaining usage or a bounded payload-free error code."""
    error: str
    try:
        model = model or resolve_deployment()
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
            raise ClassificationOutputError("refusal")
        parsed = message.parsed
        if parsed is None:
            raise ClassificationOutputError("invalid_output")
        parsed = TicketClassification.model_validate(
            parsed.model_dump() if isinstance(parsed, BaseModel) else parsed
        )
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
    except ClassificationOutputError as exc:
        error = exc.code
    except ValidationError:
        error = "invalid_output"
    except openai.LengthFinishReasonError:
        error = "truncated"
    except openai.ContentFilterFinishReasonError:
        error = "content_filter"
    except openai.BadRequestError:
        error = "bad_request"
    except openai.NotFoundError:
        error = "not_found"
    except openai.RateLimitError:
        error = "rate_limited"
    except openai.APITimeoutError:
        error = "timeout"
    except openai.APIConnectionError:
        error = "connection"
    except (
        Exception
    ) as exc:  # noqa: BLE001 - isolate a provider failure for this ticket
        error = safe_diagnostic(
            exc, operation_id=uuid4().hex, stage="fetch", key=key
        ).code
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
    texts = {}
    row_errors = []
    for position, (_, row) in enumerate(df.iterrows()):
        try:
            key, text = validate_ticket_input(row.to_dict())
        except TicketInputError as exc:
            row_errors.append({"index": position, "code": exc.code})
            continue
        texts[key] = text

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
        cache.attrs["classification_errors"] = row_errors
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
    merged.attrs["classification_errors"] = row_errors
    return merged
