"""Review regressions bind persisted identities to real SDK requests safely."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import numpy as np
import pytest
from classification_env import forbid_model_network as forbid_model_network
from openai import AzureOpenAI
from test_ticket_classifier import fake_classification, make_frame

import ticket_classifier as classifier
from classification_cache import configuration_fingerprint, load_classifications
from settings import load_settings

pytestmark = pytest.mark.usefixtures("forbid_model_network")


def _client(
    endpoint: str,
    version: str,
    calls: list[dict[str, Any]],
    *,
    azure_deployment: str | None = None,
) -> AzureOpenAI:
    def transport(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": json.loads(request.content)})
        content = dict(
            reasoning="Synthetic",
            resolution_bucket="question_answer",
            sub_bucket="how_to",
            intent_label="help",
            ai_mode="human_only",
            confidence=0.9,
        )
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "fake",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(content),
                        },
                    }
                ],
            },
        )

    # The installed SDK accepts legacy HTTPX clients at runtime; its overloads name HTTPX2 only.
    return AzureOpenAI(  # type: ignore[call-overload]
        azure_endpoint=endpoint,
        api_version=version,
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        azure_deployment=azure_deployment,
    )


def test_reusable_clients_define_distinct_effective_connection_identities(
    tmp_path: Path,
) -> None:
    """Real endpoints/API versions cannot share results despite identical environment settings."""
    calls: list[dict[str, Any]] = []
    path = tmp_path / "cache.parquet"
    clients = [
        _client("https://client-a.example", "v1", calls),
        _client("https://client-b.example", "v1", calls),
        _client("https://client-b.example", "v2", calls),
    ]
    try:
        digests = []
        for client in clients:
            result = classifier.classify_tickets(
                make_frame(["SDAX-1"]),
                path,
                model="fake",
                client=client,
                progress=False,
            )
            assert result.error.isna().all()
            digests.append(result.iloc[0].config_digest)
        assert len(calls) == len(set(digests)) == 3
        classifier.classify_tickets(
            make_frame(["SDAX-1"]),
            path,
            model="fake",
            client=clients[0],
            progress=False,
        )
        assert len(calls) == 3
    finally:
        for client in clients:
            client.close()


@pytest.mark.parametrize("mismatch", ["endpoint", "api_version", "deployment"])
def test_explicit_config_rejects_conflicting_real_client_before_cache_access(
    tmp_path: Path,
    mismatch: str,
) -> None:
    """A declared identity cannot label output from a differently routed SDK client."""
    calls: list[dict[str, Any]] = []
    client = _client("https://client-a.example", "v1", calls, azure_deployment="fake")
    config = replace(
        classifier.classifier_configuration(load_settings({}), model="fake"),
        endpoint="https://client-a.example",
        api_version="v1",
    )
    changed: dict[str, Any] = {
        "endpoint": "https://other.example",
        "api_version": "v2",
        "deployment": "other",
    }
    try:
        with patch.object(classifier, "load_cache", side_effect=AssertionError("read")) as read:
            with pytest.raises(ValueError, match="conflicting_client_configuration"):
                classifier.classify_tickets(
                    make_frame(["SDAX-1"]),
                    tmp_path / "cache.parquet",
                    client=client,
                    progress=False,
                    configuration=replace(config, **{mismatch: changed[mismatch]}),
                )
        assert not read.called and not calls
        assert not list(tmp_path.iterdir())
    finally:
        client.close()


@pytest.mark.parametrize(
    "constant",
    [
        "SYSTEM_PROMPT",
        "TEXT_RENDERER_VERSION",
        "CLASSIFICATION_POLICY_VERSION",
        "RESULT_SCHEMA_VERSION",
    ],
)
def test_stale_executable_configuration_is_rejected_before_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
) -> None:
    """Old implementation fingerprints cannot label current code or authorize cached reuse."""
    config = classifier.classifier_configuration(load_settings({}), model="fake")
    original = getattr(classifier, constant)
    monkeypatch.setattr(
        classifier,
        constant,
        original + 1 if isinstance(original, int) else original + " changed",
    )
    with patch.object(classifier, "load_cache", side_effect=AssertionError("read")) as read:
        with pytest.raises(ValueError, match="stale_implementation_configuration"):
            classifier.classify_tickets(
                make_frame(["SDAX-1"]),
                tmp_path / "cache.parquet",
                client=object(),
                progress=False,
                configuration=config,
            )
    assert not read.called


def test_nested_frozen_generation_reaches_sdk_as_ordinary_json(tmp_path: Path) -> None:
    """Recursive immutability must not make valid SDK generation bodies unserializable."""
    calls: list[dict[str, Any]] = []
    client = _client("https://client-a.example", "v1", calls)
    config = replace(
        classifier.classifier_configuration(load_settings({}), model="fake"),
        endpoint="https://client-a.example",
        api_version="v1",
        generation={
            "max_chars": 6000,
            "max_completion_tokens": 1234,
            "extra_body": {"metadata": {"experiment": "synthetic"}},
        },
    )
    before = configuration_fingerprint(config)
    try:
        result = classifier.classify_tickets(
            make_frame(["SDAX-1"]),
            tmp_path / "cache.parquet",
            client=client,
            progress=False,
            configuration=config,
        )
        assert result.error.isna().all()
        assert calls[0]["body"]["metadata"] == {"experiment": "synthetic"}
        assert configuration_fingerprint(config) == before
    finally:
        client.close()


@pytest.mark.parametrize("bad", [np.array("unsized"), "bad\ud800unicode"])
def test_malformed_unsized_or_unicode_cells_do_not_abort_valid_neighbors(
    tmp_path: Path,
    bad: object,
) -> None:
    """Malformed cells produce bounded input errors without blocking another ticket."""
    frame = make_frame(["SDAX-1", "SDAX-2"])
    frame.at[0, "summary"] = bad
    path = tmp_path / "cache.parquet"
    with patch.object(classifier, "classify_one", side_effect=fake_classification) as call:
        result = classifier.classify_tickets(frame, path, client=object(), progress=False)
    assert call.call_count == 1 and call.call_args.args[1] == "SDAX-2"
    assert result.loc[result.key == "SDAX-1", "error"].iloc[0] == "invalid_text"
    assert load_classifications(path).key.tolist() == ["SDAX-2"]
    assert result.attrs["classification_errors"] == [{"index": 0, "code": "invalid_text"}]


def test_invalid_unicode_key_is_only_a_positional_error(tmp_path: Path) -> None:
    """Unencodable keys never reach parquet or prevent a valid neighboring key."""
    frame = make_frame(["SDAX-\ud800", "SDAX-2"])
    with patch.object(classifier, "classify_one", side_effect=fake_classification) as call:
        result = classifier.classify_tickets(
            frame, tmp_path / "cache.parquet", client=object(), progress=False
        )
    assert result.key.tolist() == ["SDAX-2"] and call.call_count == 1
    assert result.attrs["classification_errors"] == [{"index": 0, "code": "invalid_key"}]
