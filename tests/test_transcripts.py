"""The recorded agent transcripts an evaluator sees without an API key.

The dashboard replays a real run so that the agent is visible to someone who
cannot run it. That only works if the recording stays true to the code, so
these tests guard the two ways it can quietly rot:

  * the tool set changes and the recording still shows the old one, and
  * the recording drifts from the batch it was made against.

Both would leave the dashboard confidently describing an agent that no longer
exists, which is exactly the failure this project refuses to make anywhere else.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from finctl.adjudicate.tools import Toolbox
from finctl.capture_agent import load_transcripts
from finctl.ingest.loader import load_batch

DATA = Path(__file__).resolve().parent.parent / "data"


class TestRecordedTranscripts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = load_transcripts(DATA)
        if cls.payload is None:
            raise unittest.SkipTest(
                "no recording committed; run `python -m finctl capture`"
            )
        cls.cases = cls.payload["cases"]
        cls.batch = load_batch(DATA)

    def test_the_recording_is_labelled_with_what_produced_it(self):
        """A recording presented without provenance is indistinguishable from a mock."""
        for field in ("recorded_at", "provider", "model"):
            self.assertTrue(self.payload.get(field), field)

    def test_it_covers_more_than_one_kind_of_case(self):
        """A single-behaviour recording is a highlight reel, not evidence."""
        reasons = {case["reason"] for case in self.cases}
        self.assertGreaterEqual(len(reasons), 4, f"only covers {reasons}")

    def test_it_shows_the_agent_both_deciding_and_declining(self):
        decisions = {case["decision"] for case in self.cases}
        self.assertIn("decline", decisions)
        self.assertIn("match", decisions)

    def test_every_tool_called_still_exists(self):
        """The recording must not advertise a tool that has since been removed."""
        available = {entry["function"]["name"] for entry in Toolbox.schema()}
        for case in self.cases:
            for step in case["steps"]:
                self.assertIn(
                    step["tool"], available,
                    f"{case['settlement_id']} calls {step['tool']}, which no longer "
                    f"exists -- re-record with `python -m finctl capture`",
                )

    def test_every_record_it_names_is_in_the_batch(self):
        """A transcript about records that are not there describes a different run."""
        settlements = self.batch.index_settlements()
        lines = self.batch.index_bank_lines()
        for case in self.cases:
            self.assertIn(case["settlement_id"], settlements)
            for candidate in case["candidates"]:
                self.assertIn(candidate["line_id"], lines)
            if case["chosen"]:
                self.assertIn(case["chosen"], lines)

    def test_every_match_carries_a_verifier_verdict(self):
        """The proposal is only half the mechanism; the veto is the other half."""
        for case in self.cases:
            if case["decision"] != "match":
                continue
            verdict = case.get("verifier", {})
            self.assertTrue(verdict.get("ran"), case["settlement_id"])
            self.assertIn("accepted", verdict)
            if not verdict["accepted"]:
                self.assertTrue(
                    verdict.get("violations"),
                    "a rejection must say which invariant failed",
                )

    def test_a_vetoed_proposal_is_present(self):
        """The guardrail firing on a real model is the point of the recording.

        If a future model stops making mistakes on this batch, this test fails
        and the honest fix is to say so in the README -- not to keep shipping a
        recording that no longer demonstrates anything.
        """
        vetoed = [
            case for case in self.cases
            if case["decision"] == "match"
            and case.get("verifier", {}).get("accepted") is False
        ]
        self.assertTrue(
            vetoed,
            "no vetoed proposal in the recording; the verifier's role is unproven",
        )

    def test_reasoning_is_present_on_every_case(self):
        for case in self.cases:
            self.assertTrue(case["reasoning"].strip(), case["settlement_id"])

    def test_amounts_are_readable_not_escaped(self):
        """Tool results are read by a human in the dashboard."""
        escaped = [
            case["settlement_id"] for case in self.cases
            for step in case["steps"]
            if "\\u20b9" in step["result"]
        ]
        self.assertEqual(escaped, [], "rupee signs are escape sequences")


if __name__ == "__main__":
    unittest.main()
