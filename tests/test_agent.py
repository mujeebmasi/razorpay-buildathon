"""The tool-using agent: its toolbox, and the rules that contain it.

The agent sits outside the trust boundary, so what matters is not that it makes
good decisions -- the verifier handles that -- but that every way it can go
wrong lands somewhere safe. These tests drive the loop against a stubbed
transport, so they need no API key, no network, and no budget, and they run in
CI exactly as they run here.

Each test names a specific way a language model misbehaves in practice.
"""
from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

from finctl.adjudicate.agent import PROVIDERS, AgentAdjudicator
from finctl.adjudicate.tools import build_toolbox
from finctl.engine.reconcile import AdjudicationRequest
from finctl.ingest.loader import load_batch

DATA = Path(__file__).resolve().parent.parent / "data"


def _tool_call(name: str, arguments: dict, call_id: str = "c1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _reply(tool_calls: list[dict] | None = None, content: str = "") -> dict:
    return {
        "choices": [{"message": {"content": content, "tool_calls": tool_calls or []}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class AgentHarness(unittest.TestCase):
    """Builds a real agent over the real batch, with a scripted transport."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.batch = load_batch(DATA)
        settlements = cls.batch.index_settlements()
        credits = [line for line in cls.batch.bank_lines if line.is_credit]
        cls.settlement = next(iter(settlements.values()))
        cls.candidates = credits[:3]

    def build(self, script: list[dict]) -> AgentAdjudicator:
        """An agent whose transport replays `script`, one reply per request."""
        agent = AgentAdjudicator(
            self.batch, provider="groq", model="stub-model", api_key="test-key",
        )
        replies = list(script)

        def fake_request(path: str, payload: dict | None = None) -> dict:
            self.assertTrue(path.endswith("/chat/completions"), path)
            if not replies:
                raise AssertionError("agent made more requests than the script allows")
            return replies.pop(0)

        agent._request = fake_request  # type: ignore[method-assign]
        return agent

    def request(self) -> AdjudicationRequest:
        return AdjudicationRequest(
            subject_kind="settlement",
            subject_id=self.settlement.settlement_id,
            subject_amount=self.settlement.amount,
            subject_date=self.settlement.settled_on,
            subject_description="test payout",
            candidates=tuple(
                {"id": line.line_id, "amount": line.amount,
                 "date": line.value_date.isoformat()}
                for line in self.candidates
            ),
        )


class TestContainment(AgentHarness):
    """Every failure mode must land on an abstention, never on a match."""

    def test_it_can_decide(self):
        """The baseline. Without this, the refusal tests prove nothing."""
        chosen = self.candidates[0].line_id
        agent = self.build([
            _reply([_tool_call("submit_decision", {
                "decision": "match", "line_id": chosen,
                "confidence": 0.9, "reasoning": "reference matched exactly",
            })]),
        ])
        outcome = agent.adjudicate(self.request())
        self.assertEqual(outcome.decision, "match")
        self.assertEqual(outcome.chosen_ids, (chosen,))
        self.assertEqual(agent.usage.decided, 1)

    def test_a_fabricated_id_is_discarded(self):
        """The model names a record it was never offered."""
        agent = self.build([
            _reply([_tool_call("submit_decision", {
                "decision": "match", "line_id": "bank_does_not_exist",
                "confidence": 1.0, "reasoning": "confident and wrong",
            })]),
        ])
        outcome = agent.adjudicate(self.request())
        self.assertEqual(outcome.decision, "abstain")
        self.assertIn("not among the candidates", outcome.rationale)
        self.assertEqual(agent.usage.decided, 0)

    def test_declining_is_a_first_class_outcome(self):
        agent = self.build([
            _reply([_tool_call("submit_decision", {
                "decision": "decline",
                "reasoning": "two candidates are supported equally",
            })]),
        ])
        outcome = agent.adjudicate(self.request())
        self.assertEqual(outcome.decision, "abstain")
        self.assertIn("equally", outcome.rationale)
        self.assertEqual(agent.usage.declined, 1)

    def test_prose_instead_of_a_decision_is_not_a_decision(self):
        """The model answers in words without calling the tool."""
        agent = self.build([_reply(content="I think it is probably the first one.")])
        outcome = agent.adjudicate(self.request())
        self.assertEqual(outcome.decision, "abstain")

    def test_running_out_of_turns_abstains(self):
        """The model investigates forever and never concludes."""
        line_id = self.candidates[0].line_id
        loop = _reply([_tool_call("get_credit", {"line_id": line_id})])
        agent = self.build([loop] * 6)
        outcome = agent.adjudicate(self.request())
        self.assertEqual(outcome.decision, "abstain")
        self.assertIn("did not reach a decision", outcome.rationale)

    def test_transport_failure_abstains(self):
        agent = self.build([])

        def boom(path, payload=None):
            raise OSError("connection reset")

        agent._request = boom  # type: ignore[method-assign]
        outcome = agent.adjudicate(self.request())
        self.assertEqual(outcome.decision, "abstain")
        self.assertIn("unreachable", outcome.rationale)
        self.assertEqual(agent.usage.failed, 1)

    def test_malformed_tool_arguments_do_not_crash_the_run(self):
        agent = self.build([
            {"choices": [{"message": {"tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_credit", "arguments": "{not json"}},
            ]}}], "usage": {}},
            _reply([_tool_call("submit_decision", {
                "decision": "decline", "reasoning": "gave up",
            })]),
        ])
        outcome = agent.adjudicate(self.request())
        self.assertEqual(outcome.decision, "abstain")

    def test_confidence_is_clamped(self):
        chosen = self.candidates[0].line_id
        agent = self.build([
            _reply([_tool_call("submit_decision", {
                "decision": "match", "line_id": chosen,
                "confidence": 42, "reasoning": "very sure",
            })]),
        ])
        outcome = agent.adjudicate(self.request())
        self.assertLessEqual(outcome.confidence, 1.0)

    def test_no_candidates_means_no_request_is_made(self):
        agent = self.build([])   # any request would blow the empty script
        outcome = agent.adjudicate(
            AdjudicationRequest("settlement", "setl_x", 100, date(2026, 7, 1), "", ())
        )
        self.assertEqual(outcome.decision, "abstain")

    def test_case_budget_stops_the_run_growing_without_bound(self):
        agent = self.build([
            _reply([_tool_call("submit_decision", {
                "decision": "decline", "reasoning": "no",
            })]),
        ])
        agent.case_budget = 1
        first = agent.adjudicate(self.request())
        second = agent.adjudicate(self.request())
        self.assertEqual(first.decision, "abstain")
        self.assertIn("budget", second.rationale)

    def test_the_tool_trace_is_kept_as_evidence(self):
        line_id = self.candidates[0].line_id
        agent = self.build([
            _reply([_tool_call("score_reference", {"line_id": line_id})]),
            _reply([_tool_call("submit_decision", {
                "decision": "decline", "reasoning": "reference absent",
            })]),
        ])
        outcome = agent.adjudicate(self.request())
        kinds = {item.kind for item in outcome.evidence}
        self.assertIn("agent_tool_call", kinds)
        self.assertIn(self.settlement.settlement_id, agent.traces)
        self.assertTrue(agent.traces[self.settlement.settlement_id])


class TestToolbox(AgentHarness):
    """The investigation surface is read-only and scoped to what was offered."""

    def toolbox(self):
        return build_toolbox(
            self.batch,
            self.settlement.settlement_id,
            [line.line_id for line in self.candidates],
        )

    def test_a_record_outside_the_candidate_set_is_refused(self):
        """Scope is what makes a fabricated id detectable rather than plausible."""
        other = next(
            line for line in self.batch.bank_lines
            if line.line_id not in {c.line_id for c in self.candidates}
        )
        result = json.loads(self.toolbox().call("get_credit", {"line_id": other.line_id}))
        self.assertIn("error", result)
        self.assertIn("not one of the candidates", result["error"])

    def test_unknown_tools_are_reported_not_raised(self):
        result = json.loads(self.toolbox().call("delete_everything", {}))
        self.assertIn("error", result)

    def test_bad_arguments_are_reported_not_raised(self):
        result = json.loads(self.toolbox().call("get_credit", {"wrong": 1}))
        self.assertIn("error", result)

    def test_gap_classification_names_the_signature(self):
        box = self.toolbox()
        result = json.loads(box.call("explain_gap", {"line_id": self.candidates[0].line_id}))
        for field in (
            "difference", "within_rounding_tolerance",
            "looks_like_100x_unit_error", "looks_like_transposed_digits",
        ):
            self.assertIn(field, result)

    def test_amounts_come_back_formatted_not_raw(self):
        """The model weighs evidence; it is never handed paise to add up."""
        box = self.toolbox()
        result = json.loads(box.call("get_credit", {"line_id": self.candidates[0].line_id}))
        self.assertIsInstance(result["amount"], str)
        self.assertIn("₹", result["amount"])

    def test_every_call_is_recorded(self):
        box = self.toolbox()
        box.call("payout_components", {})
        box.call("list_credits_near", {"days": 3})
        self.assertEqual(len(box.calls), 2)
        self.assertEqual(box.calls[0].name, "payout_components")

    def test_window_width_is_bounded(self):
        """A model asking for a 900-day window gets a sane one."""
        result = json.loads(self.toolbox().call("list_credits_near", {"days": 900}))
        self.assertLessEqual(result["window_days"], 10)

    def test_schema_is_well_formed(self):
        from finctl.adjudicate.tools import Toolbox

        for entry in Toolbox.schema():
            self.assertEqual(entry["type"], "function")
            function = entry["function"]
            self.assertTrue(function["description"].strip())
            self.assertEqual(function["parameters"]["type"], "object")
            for name in function["parameters"]["required"]:
                self.assertIn(name, function["parameters"]["properties"])


class TestProviderConfig(unittest.TestCase):
    def test_providers_are_openai_compatible_shaped(self):
        for name, provider in PROVIDERS.items():
            self.assertTrue(provider.base_url.startswith("https://"), name)
            self.assertTrue(provider.key_env.endswith("_API_KEY"), name)
            self.assertTrue(provider.prefer, name)

    def test_missing_key_refuses_to_construct(self):
        import os

        saved = os.environ.pop("GROQ_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError) as caught:
                AgentAdjudicator(load_batch(DATA), provider="groq", model="x")
            self.assertIn("GROQ_API_KEY", str(caught.exception))
        finally:
            if saved is not None:
                os.environ["GROQ_API_KEY"] = saved

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(RuntimeError):
            AgentAdjudicator(load_batch(DATA), provider="nope", api_key="k", model="x")


if __name__ == "__main__":
    unittest.main()
