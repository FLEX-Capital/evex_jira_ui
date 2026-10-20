"""Explicit Azure OpenAI classification with validated taxonomy and versioned reuse.

Only an explicit classify_tickets invocation schedules valid pending inputs.
Cache/configuration reads and ordinary dashboard visits never construct a model
client. Configure environment variables with the launcher (for example uv run
--env-file .env); application code reads only the captured environment snapshot.

AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT_NAME are
required only when pending work needs a real client. AZURE_OPENAI_API_VERSION
has the shared default. The deployment must support structured outputs.

A reusable identity contains key, full rendered-text SHA-256 and a fingerprint
of endpoint/deployment/API, exact prompt/taxonomy, schema/renderer/policy versions
and all sent generation settings plus text cap. Credentials are excluded. Bump
CLASSIFICATION_POLICY_VERSION when rebinding a deployment to another model.
Legacy rows remain unverified until a later explicit invocation classifies them.

The single versioned parquet retains independent historical identities under
transactional latest-disk merge. Public classify_tickets returns one selected
matching result/error per requested key, rather than the whole history, so
notebook joins do not multiply rows. Missing-key diagnostics use input positions.
Periodic flushes preserve completed results; interrupted in-flight work may need
an explicit retry. No automatic paid reclassification or remote discovery occurs.
"""

import hashlib
import json
from collections.abc import Mapping, Sized
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self, get_args
from uuid import uuid4

import openai
import pandas as pd
from openai import AzureOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

import settings
from classification_cache import (
    RESULT_COLUMNS,
    ClassificationIdentity,
    ClassifierConfig,
    classification_identity,
    load_classifications,
    merge_classifications,
    select_classifications,
)
from operation_errors import DIAGNOSTIC_CODES, safe_diagnostic
from settings import Settings, load_settings

CACHE_PATH = "data/sdax_classifications.parquet"

# The API-version default is centralized in settings and can be injected by the launcher.
DEFAULT_API_VERSION = settings.DEFAULT_API_VERSION


MAX_COMPLETION_TOKENS = 4000
MAX_TICKET_CHARS = 6000
LEGACY_TEXT_HASH_CHARS = 16
RESULT_SCHEMA_VERSION = 2
CLASSIFICATION_POLICY_VERSION = "1"
TEXT_RENDERER_VERSION = "1"
_SAFE_MODEL_ERROR_CODES = DIAGNOSTIC_CODES | {
    "truncated",
    "content_filter",
    "bad_request",
    "refusal",
    "invalid_output",
}


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
    """One structured resolution outcome with validated parent/sub-bucket membership."""

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


def build_client(
    deployment: str | None = None, *, settings: Settings | None = None
) -> AzureOpenAI:
    """Construct the requested deployment client from one settings snapshot."""
    connection = (settings or load_settings()).require_classifier(deployment)
    return AzureOpenAI(
        api_key=connection.api_key,
        azure_endpoint=connection.endpoint,
        api_version=connection.api_version,
        max_retries=5,
    )


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:LEGACY_TEXT_HASH_CHARS]


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


def build_ticket_text(row: Mapping[str, Any], max_chars: int = MAX_TICKET_CHARS) -> str:
    """Render summary/category/description/comments with the existing cap and suffix.

    Args:
        row: One ticket's optional text fields; no key is needed for rendering.
        max_chars: Prefix length before the existing truncation marker.

    Returns:
        Deterministic prompt text, with summary first.
    """
    return _render_ticket_text(_ticket_fields(row), max_chars)


def validate_ticket_input(
    row: Mapping[str, Any], *, max_chars: int = MAX_TICKET_CHARS
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


def classifier_configuration(
    settings: Settings, *, model: str | None = None
) -> ClassifierConfig:
    """Capture every request/reuse policy input without requiring credentials.

    Args:
        settings: Operation environment snapshot; reading it does not discover models.
        model: Optional deployment alias override.

    Returns:
        A credential-free immutable configuration for full cache identities.
    """
    taxonomy = {
        "schema": TicketClassification.model_json_schema(),
        "parents": _SUB_BUCKETS_BY_PARENT,
    }
    taxonomy_digest = hashlib.sha256(
        json.dumps(
            taxonomy, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    return ClassifierConfig(
        settings.azure_endpoint or "",
        model or settings.azure_deployment or "",
        settings.azure_api_version,
        hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        taxonomy_digest,
        RESULT_SCHEMA_VERSION,
        TEXT_RENDERER_VERSION,
        CLASSIFICATION_POLICY_VERSION,
        {"max_completion_tokens": MAX_COMPLETION_TOKENS, "max_chars": MAX_TICKET_CHARS},
    )


def _error_result(
    key: str, text_hash: str | None, model: str | None, code: str
) -> dict[str, Any]:
    result: dict[str, Any] = dict.fromkeys(RESULT_COLUMNS)
    result.update(key=key, text_hash=text_hash, model=model, error=code)
    return result


def classify_one(
    client: Any,
    key: str,
    text: str,
    model: str | None = None,
    *,
    generation: Mapping[str, object] | None = None,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Classify one ticket, retaining usage or a bounded payload-free error code."""
    error: str
    try:
        model = model or resolve_deployment()
        request_generation = (
            dict(generation)
            if generation is not None
            else {"max_completion_tokens": MAX_COMPLETION_TOKENS}
        )
        request_generation.pop("max_chars", None)
        response = client.chat.completions.parse(
            model=model,
            # Reasoning deployments (gpt-5.x) reject any temperature but the
            # default and spend part of this budget on hidden reasoning tokens,
            # so the ceiling is well above what the JSON answer alone needs.
            # Running out shows up as a LengthFinishReasonError, not silence.
            **request_generation,
            messages=[
                {
                    "role": "system",
                    "content": (
                        SYSTEM_PROMPT if system_prompt is None else system_prompt
                    ),
                },
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
    return _error_result(key, _hash_text(text), model, error)


def _selected_results(
    cache: pd.DataFrame, identities: Mapping[str, ClassificationIdentity]
) -> pd.DataFrame:
    selected = select_classifications(cache, identities)
    for index, row in selected.iterrows():
        if pd.isna(row["error"]):
            try:
                TicketClassification.model_validate(
                    {name: row[name] for name in TicketClassification.model_fields}
                )
            except ValidationError:
                for name in RESULT_COLUMNS:
                    if name not in ("key", "text_hash", "model"):
                        selected.loc[index, name] = None
                selected.loc[index, "error"] = "invalid_output"
        elif row["error"] not in _SAFE_MODEL_ERROR_CODES:
            selected.loc[index, "error"] = "unexpected"
    return selected


def classify_tickets(
    df: pd.DataFrame,
    cache_path: str | Path | None = None,
    model: str | None = None,
    max_workers: int = 8,
    limit: int | None = None,
    force: bool = False,
    save_every: int = 100,
    progress: bool = True,
    client: Any = None,
    *,
    configuration: ClassifierConfig | None = None,
) -> pd.DataFrame:
    """Explicitly classify valid pending inputs and return requested identities only.

    Args:
        df: Candidate tickets; later duplicate-key inputs take precedence.
        cache_path: Optional artifact path, otherwise captured DATA_DIR.
        model: Optional deployment override; must agree with an explicit configuration.
        max_workers: Concurrent calls outside cache transactions.
        limit: Optional maximum pending calls; cached requested matches still return.
        force: Resend valid requested rows even when a matching result exists.
        save_every: Flush this many new results with latest-disk merge.
        progress: Show an optional progress bar.
        client: Explicit fake/reusable client; otherwise constructed only for pending work.
        configuration: Full immutable endpoint/model/prompt/taxonomy/policy identity.

    Returns:
        At most one matching result or safe input-error row per requested key.
        Missing-key errors are positional diagnostics in classification_errors attrs.
        Historical/unrelated identities remain stored and are not returned for joins.
    """
    operation_settings = load_settings()
    config = configuration or classifier_configuration(operation_settings, model=model)
    if model is not None and model != config.deployment:
        raise ValueError("conflicting_model_configuration")
    for name, value in (("max_workers", max_workers), ("save_every", save_every)):
        if type(value) is not int or value < 1:
            raise ValueError(f"invalid_{name}")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("invalid_limit")
    max_chars = config.generation.get("max_chars")
    max_tokens = config.generation.get("max_completion_tokens")
    if (
        type(max_chars) is not int
        or max_chars < 1
        or type(max_tokens) is not int
        or max_tokens < 1
    ):
        raise ValueError("invalid_generation_limits")
    if {"model", "messages", "response_format"}.intersection(config.generation):
        raise ValueError("reserved_generation_setting")
    if isinstance(cache_path, str) and not cache_path.strip():
        raise ValueError("invalid_cache_path")
    cache_path = Path(
        operation_settings.paths.classifications if cache_path is None else cache_path
    ).resolve()
    captured_prompt = SYSTEM_PROMPT
    cache = load_cache(cache_path)
    texts: dict[str, str] = {}
    invalid: dict[str, str] = {}
    requested: dict[str, None] = {}
    row_errors: list[dict[str, Any]] = []
    for position, (_, row) in enumerate(df.iterrows()):
        raw_key = row.get("key")
        key = raw_key.strip() if isinstance(raw_key, str) else ""
        if key:
            requested[key] = None
        try:
            key, text = validate_ticket_input(row.to_dict(), max_chars=max_chars)
        except TicketInputError as exc:
            row_errors.append({"index": position, "code": exc.code})
            if key:
                invalid[key] = exc.code
                texts.pop(key, None)
            continue
        texts[key] = text
        invalid.pop(key, None)
    identities = {
        key: classification_identity(key, text, config) for key, text in texts.items()
    }
    selected = _selected_results(cache, identities)
    done = set(selected.loc[selected["error"].isna(), "key"])
    pending = [(key, text) for key, text in texts.items() if force or key not in done]
    if limit is not None:
        pending = pending[:limit]

    def finish(current: pd.DataFrame) -> pd.DataFrame:
        matches = _selected_results(current, identities)
        errors = [
            {
                **_error_result(key, None, config.deployment, code),
                "verification_state": "input_invalid",
            }
            for key, code in invalid.items()
        ]
        if errors:
            failed_inputs = pd.DataFrame(errors).reindex(columns=matches.columns)
            for name in (
                "completed_at",
                "confidence",
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
            ):
                failed_inputs[name] = failed_inputs[name].astype(matches[name].dtype)
            result = (
                pd.concat([matches, failed_inputs], ignore_index=True)
                if not matches.empty
                else failed_inputs
            )
        else:
            result = matches
        rank = {key: position for position, key in enumerate(requested)}
        result = result.loc[
            result["key"].map(rank).sort_values(kind="stable").index
        ].reset_index(drop=True)
        result.attrs["classification_errors"] = row_errors
        return result

    if not pending:
        print(f"Nothing to classify - {len(selected)} matching requested results.")
        return finish(cache)
    if client is None:
        connection_settings = replace(
            operation_settings,
            azure_endpoint=config.endpoint or None,
            azure_deployment=config.deployment or None,
            azure_api_version=config.api_version,
        )
        client = build_client(config.deployment, settings=connection_settings)
    estimated_tokens = sum(len(text) for _, text in pending) / 4
    print(
        f"Classifying {len(pending)} tickets with {config.deployment} "
        f"(~{estimated_tokens / 1000:.0f}k input tokens, {max_workers} workers)."
    )
    results: list[dict[str, Any]] = []
    buffered: list[dict[str, Any]] = []
    bar: Any = None
    if progress:
        try:
            from tqdm.auto import tqdm

            bar = tqdm(total=len(pending), unit="ticket")
        except ImportError:
            bar = None

    def classify_pending(key: str, text: str) -> dict[str, Any]:
        try:
            result = dict(
                classify_one(
                    client,
                    key,
                    text,
                    config.deployment,
                    generation=config.generation,
                    system_prompt=captured_prompt,
                )
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 - a failed worker cannot abort unrelated requests
            cause = safe_diagnostic(
                exc, operation_id=uuid4().hex, stage="fetch", key=key
            )
            result = _error_result(key, None, config.deployment, cause.code)
        identity = identities[key]
        result.update(
            key=key,
            model=config.deployment,
            text_hash=identity.text_digest[:LEGACY_TEXT_HASH_CHARS],
            text_digest=identity.text_digest,
            config_digest=identity.config_digest,
            completed_at=datetime.now(UTC),
            verification_state="current",
        )
        return result

    def flush() -> None:
        if buffered:
            save_cache(pd.DataFrame(buffered), cache_path)
            buffered.clear()

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(classify_pending, key, text) for key, text in pending
            ]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                buffered.append(result)
                if bar is not None:
                    bar.update(1)
                if len(buffered) >= save_every:
                    flush()
        flush()
    finally:
        if bar is not None:
            bar.close()
    successful = [record for record in results if not record["error"]]
    print(
        f"Done: {len(successful)} classified, {len(results) - len(successful)} failed."
    )
    if successful:
        totals = {
            name: sum(record[name] or 0 for record in successful)
            for name in ("input_tokens", "output_tokens", "reasoning_tokens")
        }
        print(
            f"Tokens/ticket (mean over {len(successful)}): "
            f"in {totals['input_tokens'] / len(successful):.0f}, "
            f"out {totals['output_tokens'] / len(successful):.0f} "
            f"(davon reasoning {totals['reasoning_tokens'] / len(successful):.0f})"
        )
    return finish(load_cache(cache_path))
