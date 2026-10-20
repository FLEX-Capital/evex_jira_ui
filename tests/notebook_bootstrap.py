"""Synthetic kernel-only bootstrap; no credentials or real cache paths are used."""

from typing import Any, NoReturn
from unittest.mock import patch

import httpx
import httpx2
import pandas as pd
import requests

import ticket_classifier as classifier
from settings import load_settings


def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
    """Reject every real HTTP transport before DNS or socket operations."""
    raise AssertionError("offline notebook transport")


patch.object(requests.sessions.Session, "request", forbidden).start()
for library in (httpx, httpx2):
    patch.object(library.HTTPTransport, "handle_request", forbidden).start()
    patch.object(library.AsyncHTTPTransport, "handle_async_request", forbidden).start()


async def verify_transports() -> int:
    """Verify all five guarded clients reject a synthetic URL before any socket access."""
    blocked = 0
    try:
        requests.get("https://offline-notebook.invalid")
    except AssertionError:
        blocked += 1
    for library in (httpx, httpx2):
        try:
            library.Client().get("https://offline-notebook.invalid")
        except AssertionError:
            blocked += 1
        try:
            async with library.AsyncClient() as async_client:
                await async_client.get("https://offline-notebook.invalid")
        except AssertionError:
            blocked += 1
    assert blocked == 5
    return blocked


patch.object(classifier, "build_client", forbidden).start()
fake_calls: list[str] = []


def fake_one(
    client: Any, key: str, text: str, model: str | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Return deterministic taxonomy-valid data without an SDK client."""
    return dict(
        key=key,
        text_hash=classifier._hash_text(text),
        resolution_bucket="support_todo",
        sub_bucket="data_correction",
        intent_label="synthetic correction",
        ai_mode="ai_drafts_human_sends",
        confidence=0.9,
        reasoning="Synthetic fixture",
        model=model,
        input_tokens=20,
        output_tokens=10,
        reasoning_tokens=0,
        error=None,
    )


def classification_runner(selected: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
    """Exercise the real identity-aware workflow with a fake worker."""
    fake_calls.extend(selected.key.tolist())
    with patch.object(classifier, "classify_one", side_effect=fake_one):
        return classifier.classify_tickets(selected, client=object(), progress=False, **kwargs)


configuration = classifier.classifier_configuration(load_settings(), model="synthetic")
