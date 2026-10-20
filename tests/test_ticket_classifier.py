import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pandas as pd
import pytest
from classification_env import forbid_model_network as forbid_model_network

import ticket_classifier

pytestmark = pytest.mark.usefixtures("forbid_model_network")


def make_frame(keys, summary="Rechnung stornieren"):
    return pd.DataFrame(
        {
            "key": keys,
            "summary": [summary] * len(keys),
            "description": [""] * len(keys),
            "comments": [""] * len(keys),
            "Hauptkategorie": ["Stammdaten"] * len(keys),
            "Unterkategorie": ["NA"] * len(keys),
        }
    )


def fake_classification(
    client: Any, key: str, text: str, model: str | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Return a valid synthetic classification with token usage."""
    return {
        "key": key,
        "text_hash": ticket_classifier._hash_text(text),
        "resolution_bucket": "support_todo",
        "sub_bucket": "data_correction",
        "intent_label": "rechnung stornieren",
        "ai_mode": "ai_drafts_human_sends",
        "confidence": 0.9,
        "reasoning": "Routine correction.",
        "model": model,
        "input_tokens": 900,
        "output_tokens": 300,
        "reasoning_tokens": 180,
        "error": None,
    }


class TicketTextTests(unittest.TestCase):
    def test_empty_optional_fields_are_omitted(self):
        row = {
            "summary": "Kasse startet nicht",
            "description": "nan",
            "comments": "",
            "Hauptkategorie": "Computerkasse",
            "Unterkategorie": "NA",
        }
        text = ticket_classifier.build_ticket_text(row)
        self.assertIn("Summary: Kasse startet nicht", text)
        self.assertIn("Category: Computerkasse", text)
        # "NA" is the placeholder 74% of Unterkategorie values carry, and the
        # string "nan" is what a missing description becomes via str().
        self.assertNotIn("NA", text)
        self.assertNotIn("Description", text)
        self.assertNotIn("Comments", text)

    def test_long_comments_are_truncated_but_summary_survives(self):
        row = {
            "summary": "Fehler beim Abrechnen",
            "description": "",
            "comments": "x" * 20000,
            "Hauptkategorie": "Abrechnung Kostenträger",
            "Unterkategorie": "TSE",
        }
        text = ticket_classifier.build_ticket_text(row, max_chars=500)
        self.assertLess(len(text), 600)
        self.assertIn("Fehler beim Abrechnen", text)
        self.assertIn("Abrechnung Kostenträger / TSE", text)
        self.assertTrue(text.endswith("[...truncated]"))


class CacheTests(unittest.TestCase):
    def test_second_run_classifies_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / "c.parquet")
            frame = make_frame(["SDAX-1", "SDAX-2"])
            with patch.object(
                ticket_classifier, "classify_one", side_effect=fake_classification
            ) as call:
                ticket_classifier.classify_tickets(
                    frame, cache_path=cache, progress=False, client=object()
                )
                self.assertEqual(call.call_count, 2)
                ticket_classifier.classify_tickets(
                    frame, cache_path=cache, progress=False, client=object()
                )
                self.assertEqual(call.call_count, 2)

    def test_only_new_and_changed_tickets_are_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / "c.parquet")
            with patch.object(
                ticket_classifier, "classify_one", side_effect=fake_classification
            ) as call:
                ticket_classifier.classify_tickets(
                    make_frame(["SDAX-1", "SDAX-2"]),
                    cache_path=cache,
                    progress=False,
                    client=object(),
                )
                call.reset_mock()

                # SDAX-2's text changed, SDAX-3 is new, SDAX-1 is untouched.
                frame = make_frame(["SDAX-1", "SDAX-2", "SDAX-3"])
                frame.loc[frame["key"] == "SDAX-2", "summary"] = "Etwas anderes"
                merged = ticket_classifier.classify_tickets(
                    frame, cache_path=cache, progress=False, client=object()
                )

            sent = sorted(c.args[1] for c in call.call_args_list)
            self.assertEqual(sent, ["SDAX-2", "SDAX-3"])
            self.assertEqual(len(merged), 3)
            self.assertEqual(merged["key"].nunique(), 3)

    def test_failed_tickets_are_retried_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / "c.parquet")
            frame = make_frame(["SDAX-1"])

            def failing(
                client: Any,
                key: str,
                text: str,
                model: str | None = None,
                **kwargs: Any,
            ) -> dict[str, Any]:
                result = fake_classification(client, key, text, model)
                result.update(resolution_bucket=None, error="connection")
                return result

            with patch.object(ticket_classifier, "classify_one", side_effect=failing):
                ticket_classifier.classify_tickets(
                    frame, cache_path=cache, progress=False, client=object()
                )
            with patch.object(
                ticket_classifier, "classify_one", side_effect=fake_classification
            ) as call:
                merged = ticket_classifier.classify_tickets(
                    frame, cache_path=cache, progress=False, client=object()
                )
                self.assertEqual(call.call_count, 1)
            self.assertEqual(merged.loc[0, "resolution_bucket"], "support_todo")
            self.assertIsNone(merged.loc[0, "error"])

    def test_force_reclassifies_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / "c.parquet")
            frame = make_frame(["SDAX-1", "SDAX-2"])
            with patch.object(
                ticket_classifier, "classify_one", side_effect=fake_classification
            ) as call:
                ticket_classifier.classify_tickets(
                    frame, cache_path=cache, progress=False, client=object()
                )
                call.reset_mock()
                ticket_classifier.classify_tickets(
                    frame, cache_path=cache, progress=False, force=True, client=object()
                )
                self.assertEqual(call.call_count, 2)


class TaxonomyTests(unittest.TestCase):
    """The prompt spells the taxonomy out in prose; the tuples drive the schema.

    They are two copies of the same list, so guard against them drifting apart.
    """

    def test_prompt_names_every_declared_bucket(self):
        for bucket in ticket_classifier.RESOLUTION_BUCKETS:
            self.assertIn(bucket, ticket_classifier.SYSTEM_PROMPT, bucket)

    def test_prompt_names_every_declared_sub_bucket_and_mode(self):
        for name in ticket_classifier.SUB_BUCKETS + ticket_classifier.AI_MODES:
            self.assertIn(name, ticket_classifier.SYSTEM_PROMPT, name)


class FakeClient:
    """Stands in for AzureOpenAI: client.chat.completions.parse(...)."""

    def __init__(self, result):
        self._result = result
        outer = self

        class Completions:
            @staticmethod
            def parse(**kwargs):
                if isinstance(outer._result, Exception):
                    raise outer._result
                return outer._result

        self.chat = type("Chat", (), {"completions": Completions})()


def make_response(parsed=None, refusal=None):
    message = type("Message", (), {"parsed": parsed, "refusal": refusal})()
    choice = type("Choice", (), {"message": message})()
    return type("Response", (), {"choices": [choice]})()


class ApiErrorTests(unittest.TestCase):
    def test_api_failure_is_recorded_not_raised(self):
        result = ticket_classifier.classify_one(
            FakeClient(RuntimeError("connection reset")), "SDAX-9", "Summary: x"
        )
        self.assertIsNone(result["resolution_bucket"])
        self.assertEqual(result["error"], "unexpected")
        self.assertEqual(result["key"], "SDAX-9")

    def test_wrong_deployment_is_reported_with_the_variable_to_fix(self):
        import openai

        error = openai.NotFoundError(
            "deployment not found",
            response=httpx.Response(404, request=httpx.Request("POST", "http://x")),
            body=None,
        )
        result = ticket_classifier.classify_one(FakeClient(error), "SDAX-9", "x")
        self.assertEqual(result["error"], "not_found")

    def test_content_filter_refusal_is_recorded_not_retried_forever(self):
        response = make_response(parsed=None, refusal="Ich kann das nicht beantworten.")
        result = ticket_classifier.classify_one(FakeClient(response), "SDAX-9", "x")
        self.assertIsNone(result["resolution_bucket"])
        self.assertIn("refusal", result["error"])

    def test_empty_parse_is_an_error_not_a_silent_none_row(self):
        result = ticket_classifier.classify_one(
            FakeClient(make_response(parsed=None)), "SDAX-9", "x"
        )
        self.assertEqual(result["error"], "invalid_output")

    def test_successful_parse_is_unpacked(self):
        parsed = ticket_classifier.TicketClassification(
            reasoning="Routine correction.",
            resolution_bucket="support_todo",
            sub_bucket="data_correction",
            intent_label="rechnung stornieren",
            ai_mode="ai_drafts_human_sends",
            confidence=0.9,
        )
        result = ticket_classifier.classify_one(
            FakeClient(make_response(parsed=parsed)), "SDAX-9", "x"
        )
        self.assertIsNone(result["error"])
        self.assertEqual(result["resolution_bucket"], "support_todo")
        self.assertEqual(result["intent_label"], "rechnung stornieren")


class ConfigurationTests(unittest.TestCase):
    def test_missing_configuration_names_every_missing_variable(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(RuntimeError) as caught,
        ):
            ticket_classifier.build_client()
        message = str(caught.exception)
        for name in (
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_DEPLOYMENT_NAME",
        ):
            self.assertIn(name, message)


if __name__ == "__main__":
    unittest.main()


class DeploymentResolutionTests(unittest.TestCase):
    """Resolution happens per call, so load_dotenv() after import still works."""

    def test_name_suffixed_variable_wins(self):
        with patch.dict(
            os.environ,
            {"AZURE_OPENAI_DEPLOYMENT_NAME": "a", "AZURE_OPENAI_DEPLOYMENT": "b"},
            clear=True,
        ):
            self.assertEqual(ticket_classifier.resolve_deployment(), "a")

    def test_unsuffixed_variable_is_accepted(self):
        with patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "b"}, clear=True):
            self.assertEqual(ticket_classifier.resolve_deployment(), "b")

    def test_env_set_after_import_is_still_seen(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ticket_classifier.resolve_deployment(), "")
        with patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT_NAME": "later"}, clear=True):
            self.assertEqual(ticket_classifier.resolve_deployment(), "later")


class UsageTests(unittest.TestCase):
    def test_usage_is_recorded_for_cost_estimation(self):
        parsed = ticket_classifier.TicketClassification(
            reasoning="Routine correction.",
            resolution_bucket="support_todo",
            sub_bucket="data_correction",
            intent_label="rechnung stornieren",
            ai_mode="ai_drafts_human_sends",
            confidence=0.9,
        )
        response = make_response(parsed=parsed)
        details = type("Details", (), {"reasoning_tokens": 256})()
        response.usage = type(
            "Usage",
            (),
            {
                "prompt_tokens": 1024,
                "completion_tokens": 400,
                "completion_tokens_details": details,
            },
        )()
        result = ticket_classifier.classify_one(FakeClient(response), "SDAX-9", "x")
        self.assertEqual(result["input_tokens"], 1024)
        self.assertEqual(result["output_tokens"], 400)
        self.assertEqual(result["reasoning_tokens"], 256)

    def test_missing_usage_does_not_break_classification(self):
        parsed = ticket_classifier.TicketClassification(
            reasoning="x",
            resolution_bucket="support_todo",
            sub_bucket="data_correction",
            intent_label="x",
            ai_mode="human_only",
            confidence=0.5,
        )
        result = ticket_classifier.classify_one(
            FakeClient(make_response(parsed=parsed)), "SDAX-9", "x"
        )
        self.assertIsNone(result["error"])
        self.assertIsNone(result["input_tokens"])
