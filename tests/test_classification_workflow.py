"""Explicit classification reuses only matching identities without eager clients."""

import asyncio
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pandas as pd
import pytest
from classification_env import forbid_model_network as forbid_model_network
from test_classification_cache import _record
from test_ticket_classifier import (
    FakeClient,
    fake_classification,
    make_frame,
    make_response,
)

import ticket_classifier as classifier
from classification_cache import (
    classification_identity,
    load_classifications,
    merge_classifications,
)
from settings import load_settings

pytestmark = pytest.mark.usefixtures("forbid_model_network")


def _config() -> Any:
    return classifier.classifier_configuration(load_settings({}), model="fake")


def _fake(
    client: Any, key: str, text: str, model: str | None = None, **kwargs: Any
) -> dict[str, Any]:
    return fake_classification(client, key, text, model)


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": "https://other.test"},
        {"deployment": "other"},
        {"api_version": "other"},
        {"generation": {"max_completion_tokens": 5000, "max_chars": 6000}},
    ],
)
def test_same_identity_reuses_but_each_config_change_is_pending(
    tmp_path: Path,
    changes: dict[str, Any],
) -> None:
    """Configuration changes affect requested rows; historical matches remain reusable."""
    path, frame, config = (
        tmp_path / "cache.parquet",
        make_frame(["SDAX-1", "SDAX-2"]),
        _config(),
    )
    with patch.object(classifier, "classify_one", side_effect=_fake) as call:
        first = classifier.classify_tickets(
            frame, path, client=object(), progress=False, configuration=config
        )
        assert call.call_count == 2 and first.key.tolist() == ["SDAX-1", "SDAX-2"]
        call.reset_mock()
        classifier.classify_tickets(
            frame, path, client=object(), progress=False, configuration=config
        )
        assert not call.called
        changed = replace(config, **changes)
        result = classifier.classify_tickets(
            frame.iloc[:1], path, client=object(), progress=False, configuration=changed
        )
        assert call.call_count == 1 and result.key.tolist() == ["SDAX-1"]
        assert (
            result.iloc[0].config_digest
            == classification_identity(
                "SDAX-1", classifier.build_ticket_text(frame.iloc[0].to_dict()), changed
            ).config_digest
        )
        classifier.classify_tickets(
            frame.iloc[:1],
            path,
            client=object(),
            progress=False,
            configuration=changed,
            force=True,
        )
        assert call.call_count == 2
        call.reset_mock()
        classifier.classify_tickets(
            frame, path, client=object(), progress=False, configuration=config
        )
        assert not call.called
    assert len(load_classifications(path)) == 3


@pytest.mark.parametrize(
    "constant",
    [
        "SYSTEM_PROMPT",
        "TicketClassification",
        "RESULT_SCHEMA_VERSION",
        "TEXT_RENDERER_VERSION",
        "CLASSIFICATION_POLICY_VERSION",
    ],
)
def test_changed_implementation_selects_new_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
) -> None:
    """Actual implementation changes invalidate only the matching requested identity."""
    path, frame, config = (
        tmp_path / "cache.parquet",
        make_frame(["SDAX-1", "SDAX-2"]),
        _config(),
    )
    with patch.object(classifier, "classify_one", side_effect=_fake) as call:
        classifier.classify_tickets(
            frame, path, client=object(), progress=False, configuration=config
        )
        with monkeypatch.context() as change:
            original = getattr(classifier, constant)
            if constant == "TicketClassification":
                schema = original.model_json_schema()
                change.setattr(
                    original,
                    "model_json_schema",
                    lambda: {**schema, "title": "Changed"},
                )
            else:
                change.setattr(
                    classifier,
                    constant,
                    (original + 1 if isinstance(original, int) else original + " changed"),
                )
            changed = _config()
            call.reset_mock()
            result = classifier.classify_tickets(
                frame.iloc[:1],
                path,
                client=object(),
                progress=False,
                configuration=changed,
            )
            assert call.call_count == 1 and result.key.tolist() == ["SDAX-1"]
            assert (
                result.iloc[0].config_digest
                != classification_identity("SDAX-1", "unused", config).config_digest
            )
        call.reset_mock()
        classifier.classify_tickets(
            frame, path, client=object(), progress=False, configuration=config
        )
        assert not call.called
    assert len(load_classifications(path)) == 3


def test_cached_or_invalid_only_batch_does_not_construct_client(tmp_path: Path) -> None:
    """Cached, empty and invalid-only calls avoid credentials/client/discovery work."""
    path, frame, config = tmp_path / "cache.parquet", make_frame(["SDAX-1"]), _config()
    with patch.object(classifier, "classify_one", side_effect=_fake):
        classifier.classify_tickets(
            frame, path, client=object(), progress=False, configuration=config
        )
    before = path.read_bytes()
    with patch.object(classifier, "build_client", side_effect=AssertionError("eager client")):
        result = classifier.classify_tickets(frame, path, progress=False, configuration=config)
        assert result.key.tolist() == ["SDAX-1"]
        assert classifier.classify_tickets(
            pd.DataFrame(columns=["key"]), path, progress=False, configuration=config
        ).empty
        invalid = classifier.classify_tickets(
            pd.DataFrame([{"key": None, "summary": None}, {"key": "SDAX-3", "summary": None}]),
            path,
            progress=False,
            configuration=config,
        )
    assert invalid.key.tolist() == ["SDAX-3"] and invalid.iloc[0].error == "empty_text"
    assert invalid.attrs["classification_errors"] == [
        {"index": 0, "code": "invalid_key"},
        {"index": 1, "code": "empty_text"},
    ]
    assert path.read_bytes() == before


def test_result_selection_does_not_duplicate_notebook_joins(tmp_path: Path) -> None:
    """Return only current requested results/errors, retaining other histories on disk."""
    path, config = tmp_path / "cache.parquet", _config()
    frame = make_frame(["SDAX-1"])
    text = classifier.build_ticket_text(frame.iloc[0].to_dict())
    rows = [
        _record(text=text, config=config, label="requested-current"),
        _record(text="old", config=config),
        _record(text=text, config=replace(config, deployment="old-model")),
        _record(key="SDAX-2", text=text, config=config),
    ]
    merge_classifications(path, pd.DataFrame(rows))
    requested = pd.concat(
        [frame, pd.DataFrame([{"key": "SDAX-3", "summary": ""}])], ignore_index=True
    )
    with patch.object(classifier, "build_client", side_effect=AssertionError("client")):
        result = classifier.classify_tickets(requested, path, progress=False, configuration=config)
    assert result.key.tolist() == ["SDAX-1", "SDAX-3"] and result.key.is_unique
    assert (
        result.iloc[0].intent_label == "requested-current" and result.iloc[1].error == "empty_text"
    )
    assert len(requested.merge(result, on="key", validate="one_to_one")) == 2
    assert len(load_classifications(path)) == 4


def test_flush_observes_a_concurrent_writer(tmp_path: Path) -> None:
    """The model boundary holds no cache lock and flush retains a newly published peer."""
    path, config = tmp_path / "cache.parquet", _config()

    def fake(
        client: Any, key: str, text: str, model: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        probe = (
            "from pathlib import Path\nfrom file_io import path_lock\nimport sys\n"
            "with path_lock(Path(sys.argv[1]), blocking=False):\n    print('free')"
        )
        check = subprocess.run(
            [sys.executable, "-c", probe, str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert check.returncode == 0 and check.stdout.strip() == "free"
        merge_classifications(path, pd.DataFrame([_record(key="SDAX-99", config=config)]))
        return _fake(client, key, text, model)

    with patch.object(classifier, "classify_one", side_effect=fake):
        result = classifier.classify_tickets(
            make_frame(["SDAX-1"]),
            path,
            client=object(),
            progress=False,
            save_every=1,
            configuration=config,
        )
    assert result.key.tolist() == ["SDAX-1"]
    assert set(load_classifications(path).key) == {"SDAX-1", "SDAX-99"}


def test_later_duplicate_input_text_controls_the_identity(tmp_path: Path) -> None:
    """Duplicate keys emit one result for the later input, including later validation failure."""
    path, config = tmp_path / "cache.parquet", _config()
    frame = make_frame(["SDAX-1", "SDAX-1"])
    frame.loc[1, "summary"] = "Later authoritative text"
    with patch.object(classifier, "classify_one", side_effect=_fake) as call:
        result = classifier.classify_tickets(
            frame, path, client=object(), progress=False, configuration=config
        )
    assert call.call_count == 1 and "Later authoritative text" in call.call_args.args[2]
    assert len(result) == 1
    frame.loc[1, ["summary", "description", "comments"]] = ""
    with patch.object(classifier, "build_client", side_effect=AssertionError("client")):
        invalid = classifier.classify_tickets(frame, path, progress=False, configuration=config)
    assert len(invalid) == 1 and invalid.iloc[0].error == "empty_text"


def test_sent_generation_and_text_cap_match_the_configuration(tmp_path: Path) -> None:
    """The identity's generation settings actually govern the emitted request and text."""
    config = replace(
        _config(),
        generation={"max_chars": 50, "max_completion_tokens": 1234, "temperature": 1.0},
    )
    parsed = classifier.TicketClassification(
        reasoning="Synthetic",
        resolution_bucket="question_answer",
        sub_bucket="how_to",
        intent_label="hilfe finden",
        ai_mode="human_only",
        confidence=0.9,
    )
    client = FakeClient(make_response(parsed=parsed))
    with patch.object(
        client.chat.completions, "parse", wraps=client.chat.completions.parse
    ) as parse:
        result = classifier.classify_tickets(
            make_frame(["SDAX-1"], summary="x" * 100),
            tmp_path / "cache.parquet",
            client=client,
            progress=False,
            configuration=config,
        )
    sent = parse.call_args.kwargs
    assert sent["max_completion_tokens"] == 1234 and sent["temperature"] == 1.0
    assert "max_chars" not in sent
    assert sent["messages"][1]["content"].endswith("\n[...truncated]")
    assert (
        result.iloc[0].config_digest
        == classification_identity(
            "SDAX-1",
            classifier.build_ticket_text(
                make_frame(["SDAX-1"], summary="x" * 100).iloc[0].to_dict(),
                max_chars=50,
            ),
            config,
        ).config_digest
    )


def test_httpx_model_transports_cannot_reach_network() -> None:
    """Both sync/async real HTTPX transports are blocked before a connection opens."""
    with (
        httpx.Client() as client,
        pytest.raises(AssertionError, match="Unmocked model"),
    ):
        client.get("https://synthetic.invalid")

    async def attempt() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(AssertionError, match="Unmocked model"):
                await client.get("https://synthetic.invalid")

    asyncio.run(attempt())


def test_client_uses_the_captured_operation_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Environment changes after cache read cannot split identity from its endpoint/credentials."""
    environment = {
        "AZURE_OPENAI_ENDPOINT": "https://first.example",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "first",
        "AZURE_OPENAI_API_KEY": "SYNTHETIC_A",
        "AZURE_OPENAI_API_VERSION": "2024-10-21",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    original_load = classifier.load_cache

    def read(path: Any) -> pd.DataFrame:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://second.example")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "second")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "SYNTHETIC_B")
        return original_load(path)

    parsed = classifier.TicketClassification(
        reasoning="Synthetic",
        resolution_bucket="question_answer",
        sub_bucket="how_to",
        intent_label="hilfe finden",
        ai_mode="human_only",
        confidence=0.9,
    )
    with (
        patch.object(classifier, "load_cache", side_effect=read),
        patch.object(
            classifier,
            "AzureOpenAI",
            return_value=FakeClient(make_response(parsed=parsed)),
        ) as constructor,
    ):
        result = classifier.classify_tickets(
            make_frame(["SDAX-1"]), tmp_path / "cache.parquet", progress=False
        )
    assert constructor.call_count == 1
    assert constructor.call_args.kwargs["azure_endpoint"] == "https://first.example"
    assert constructor.call_args.kwargs["api_key"] == "SYNTHETIC_A"
    expected = classifier.classifier_configuration(load_settings(environment))
    from classification_cache import configuration_fingerprint

    assert result.iloc[0].config_digest == configuration_fingerprint(expected)


def test_dashboard_visit_does_not_construct_model_client(public_ui: None) -> None:
    """The ordinary empty dashboard remains offline and independent of model construction."""
    from streamlit.testing.v1 import AppTest

    original_main = sys.modules["__main__"]
    try:
        with (
            patch.object(
                classifier, "AzureOpenAI", side_effect=AssertionError("model client")
            ) as constructor,
            patch("data_loading.load_data", return_value=pd.DataFrame()),
        ):
            app = AppTest.from_file("app.py", default_timeout=30).run()
        assert not app.exception and not constructor.called
    finally:
        sys.modules["__main__"] = original_main


def test_blank_explicit_cache_path_is_not_a_default_write() -> None:
    """An invalid explicit path cannot silently select the configured artifact."""
    with patch.object(
        classifier, "load_cache", side_effect=AssertionError("unexpected cache read")
    ):
        with pytest.raises(ValueError, match="invalid_cache_path"):
            classifier.classify_tickets(
                make_frame(["SDAX-1"]),
                cache_path="",
                client=object(),
                configuration=_config(),
                progress=False,
            )
