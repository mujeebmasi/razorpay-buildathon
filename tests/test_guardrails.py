"""The guardrails, and the end-to-end run.

The claim this project makes is that a reasoning component cannot put a wrong
number into the books, because the arithmetic sits downstream of it with the
power to veto. A claim like that is only worth anything if it is tested by
something actively trying to break it, so `RecklessAdjudicator` below does
exactly what a badly-behaved model would: it confidently matches the first
candidate it is offered, every single time, with maximum confidence.

The test then asserts that none of that reaches the ledger.
"""
from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from finctl.adjudicate.offline import OfflineAdjudicator
from finctl.engine.reconcile import (
    AdjudicationRequest, AdjudicationResult, ReconConfig,
)
from finctl.ingest.loader import load_batch, load_truth
from finctl.models import Evidence, Match, ReasonCode, Tier
from finctl.pipeline import run
from finctl.verify.invariants import Invariant, Verifier

DATA = Path(__file__).resolve().parent.parent / "data"


class RecklessAdjudicator:
    """An adjudicator with no judgement, to prove the verifier has some.

    It always matches, always to the first candidate, always at full
    confidence, and always with a plausible-sounding rationale. This is the
    failure mode that matters: not a model that errors, but one that is
    confidently and articulately wrong.
    """

    name = "reckless-test-adjudicator"
    kind = "adversarial test double"

    def adjudicate(self, request: AdjudicationRequest) -> AdjudicationResult:
        if not request.candidates:
            return AdjudicationResult("abstain", (), 0.0, "nothing to choose from")
        chosen = request.candidates[0]["id"]
        return AdjudicationResult(
            decision="match",
            chosen_ids=(chosen,),
            confidence=1.0,
            rationale=(
                "The value date aligns with the settlement cycle and the narration "
                "is consistent with a gateway payout, so this is the corresponding "
                "credit."
            ),
            evidence=(
                Evidence("fabricated", "asserted without arithmetic support", 1.0,
                         (request.subject_id, chosen)),
            ),
        )


def _match(**overrides) -> Match:
    """A match with sensible defaults, for poking one invariant at a time."""
    defaults = dict(
        bank_line_ids=["bank_x"], settlement_ids=["setl_x"],
        tier=Tier.T7_ADJUDICATED, reason=ReasonCode.ADJUDICATED_MATCH,
        confidence=0.9, bank_total=100000, expected_total=100000,
        evidence=[Evidence("test", "supporting", 1.0, ("setl_x",))],
    )
    defaults.update(overrides)
    return Match.create(**defaults)


class TestVerifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch = load_batch(DATA)
        cls.verifier = Verifier(cls.batch)

        # A settlement and a credit of equal value that really do correspond,
        # so a test can start from a match the verifier would accept and break
        # exactly one property at a time.
        credits_by_amount = {
            line.amount: line for line in cls.batch.bank_lines if line.is_credit
        }
        cls.good_settlement, cls.good_line = next(
            (settlement, credits_by_amount[settlement.amount])
            for settlement in cls.batch.settlements
            if settlement.amount in credits_by_amount
        )
        # ...and a credit that does not correspond to it.
        cls.settlement = cls.good_settlement
        cls.line = next(
            line for line in cls.batch.bank_lines
            if line.is_credit and line.amount != cls.settlement.amount
        )

    def _good_match(self, **overrides):
        """A match the verifier accepts, as a baseline to perturb."""
        defaults = dict(
            bank_line_ids=[self.good_line.line_id],
            settlement_ids=[self.good_settlement.settlement_id],
            bank_total=self.good_line.amount,
            expected_total=self.good_settlement.amount,
        )
        defaults.update(overrides)
        return _match(**defaults)

    def test_accepts_a_well_formed_match(self):
        # The baseline must pass, or every rejection test below is vacuous.
        report = self.verifier.verify([self._good_match()])
        self.assertEqual(len(report.accepted), 1, report.violations)

    def test_rejects_a_match_that_does_not_balance(self):
        proposal = _match(
            bank_line_ids=[self.line.line_id],
            settlement_ids=[self.settlement.settlement_id],
            bank_total=self.line.amount,
            expected_total=self.settlement.amount,
        )
        report = self.verifier.verify([proposal])
        self.assertEqual(report.accepted, [])
        self.assertIn(
            Invariant.BALANCES, {v.invariant for v in report.violations}
        )

    def test_rejects_references_to_records_that_do_not_exist(self):
        report = self.verifier.verify([_match(bank_line_ids=["bank_does_not_exist"])])
        self.assertEqual(report.accepted, [])
        self.assertIn(
            Invariant.RECORDS_EXIST, {v.invariant for v in report.violations}
        )

    def test_rejects_a_match_with_no_supporting_evidence(self):
        report = self.verifier.verify([
            self._good_match(
                evidence=[Evidence("doubt", "counts against", -1.0, ())]
            )
        ])
        self.assertEqual(report.accepted, [])
        self.assertIn(
            Invariant.HAS_EVIDENCE, {v.invariant for v in report.violations}
        )

    def test_rejects_double_claiming_the_same_record(self):
        # Two settlements of identical value both claiming one credit. Both
        # matches balance on their own, so double-claiming is the only thing
        # wrong with the pair -- which is exactly what this check must catch.
        by_amount: dict[int, list] = {}
        for settlement in self.batch.settlements:
            by_amount.setdefault(settlement.amount, []).append(settlement)

        credits = {line.amount for line in self.batch.bank_lines if line.is_credit}
        pair_amount = next(
            (amount for amount, group in by_amount.items()
             if len(group) >= 2 and amount in credits),
            None,
        )
        self.assertIsNotNone(
            pair_amount, "batch should contain an identical-amount payout pair"
        )
        first, second = by_amount[pair_amount][:2]
        line = next(
            l for l in self.batch.bank_lines
            if l.is_credit and l.amount == pair_amount
        )

        report = self.verifier.verify([
            _match(
                bank_line_ids=[line.line_id], settlement_ids=[first.settlement_id],
                bank_total=line.amount, expected_total=first.amount,
            ),
            _match(
                bank_line_ids=[line.line_id], settlement_ids=[second.settlement_id],
                bank_total=line.amount, expected_total=second.amount,
            ),
        ])
        self.assertIn(
            Invariant.NO_DOUBLE_CLAIM, {v.invariant for v in report.violations}
        )
        self.assertEqual(len(report.accepted), 1, "one of the two must be dropped")

    def test_rejects_a_confidence_outside_zero_to_one(self):
        report = self.verifier.verify([self._good_match(confidence=1.4)])
        self.assertIn(
            Invariant.CONFIDENCE_SANE, {v.invariant for v in report.violations}
        )

    def test_verifier_recomputes_rather_than_trusting_stated_totals(self):
        # A match that lies about its own totals in a self-consistent way must
        # still be caught, because the verifier goes back to the records.
        report = self.verifier.verify([
            _match(
                bank_line_ids=[self.line.line_id],
                settlement_ids=[self.settlement.settlement_id],
                bank_total=999, expected_total=999,   # internally consistent, false
            )
        ])
        self.assertEqual(report.accepted, [])


class TestRecklessAdjudicatorIsContained(unittest.TestCase):
    """The headline guarantee, tested adversarially."""

    @classmethod
    def setUpClass(cls):
        cls.result = run(DATA, adjudicator=RecklessAdjudicator())
        cls.truth = load_truth(DATA)

    def test_the_reckless_adjudicator_did_propose_matches(self):
        # If it proposed nothing, the rest of this class proves nothing.
        self.assertGreater(
            self.result.recon.counters.get("adjudicated_matches", 0), 0,
            "the adversarial adjudicator must actually have made proposals",
        )

    def test_its_bad_proposals_were_rejected_by_arithmetic(self):
        self.assertGreater(
            self.result.verification.adjudicated_rejections, 0,
            "the verifier should have overruled the reckless proposals",
        )

    def test_no_unbalanced_match_survived_to_the_ledger(self):
        for match in self.result.matches:
            self.assertLessEqual(
                abs(match.residual), 5,
                f"{match.match_id} reached the ledger without balancing",
            )

    def test_the_journal_still_balances(self):
        self.assertTrue(self.result.posting.balances)

    def test_false_match_rate_stays_at_zero(self):
        # The whole point: a confidently wrong reasoning layer cannot raise it.
        card = self.result.scorecard
        self.assertIsNotNone(card)
        self.assertEqual(
            card.false_match_rate, 0.0,
            "a reckless adjudicator managed to get a wrong match into the output",
        )

    def test_rejections_become_visible_exceptions(self):
        rejected = [
            exception for exception in self.result.exceptions
            if exception.reason is ReasonCode.VERIFIER_REJECTED
        ]
        self.assertEqual(len(rejected), self.result.verification.rejection_count)
        for exception in rejected:
            # The discarded reasoning must be preserved for the human.
            self.assertTrue(
                any("rationale" in item.kind for item in exception.evidence),
                "a rejected proposal must keep its reasoning for review",
            )


class TestOfflineAdjudicator(unittest.TestCase):
    def setUp(self):
        self.adjudicator = OfflineAdjudicator()

    def _request(self, *candidates) -> AdjudicationRequest:
        return AdjudicationRequest(
            subject_kind="settlement", subject_id="setl_1",
            subject_amount=100000, subject_date=date(2026, 7, 15),
            subject_description="test payout", candidates=tuple(candidates),
        )

    def test_abstains_when_two_candidates_are_indistinguishable(self):
        # Both carry the same reference and the same amount -- the duplicate
        # UTR case. Each scores highly on its own; neither is distinguishable.
        twin = dict(amount=100000, date="2026-07-15",
                    narration="NEFT-AXISP00123456789-RAZORPAY",
                    delta=0, reference_score=1.0,
                    reference_mechanism="exact_substring", banking_days_late=0)
        outcome = self.adjudicator.adjudicate(
            self._request({**twin, "id": "a"}, {**twin, "id": "b"})
        )
        self.assertEqual(outcome.decision, "abstain")
        self.assertIn("too close", outcome.rationale)

    def test_decides_when_one_candidate_is_clearly_better(self):
        strong = dict(id="a", amount=100000, date="2026-07-15", delta=0,
                      narration="NEFT-AXISP00123456789-RAZORPAY",
                      reference_score=1.0, reference_mechanism="exact_substring",
                      banking_days_late=0)
        weak = dict(id="b", amount=250000, date="2026-07-15", delta=150000,
                    narration="INTEREST CREDIT", reference_score=0.0,
                    reference_mechanism="no_match", banking_days_late=0)
        outcome = self.adjudicator.adjudicate(self._request(strong, weak))
        self.assertEqual(outcome.decision, "match")
        self.assertEqual(outcome.chosen_ids, ("a",))

    def test_refuses_a_candidate_that_is_a_unit_error(self):
        outcome = self.adjudicator.adjudicate(self._request(dict(
            id="a", amount=1000, date="2026-07-15", delta=-99000,
            narration="NEFT-AXISP00123456789-RAZORPAY", reference_score=1.0,
            reference_mechanism="exact_substring", banking_days_late=0,
            scale_error=True,
        )))
        self.assertEqual(outcome.decision, "abstain")

    def test_abstains_with_no_candidates(self):
        self.assertEqual(self.adjudicator.adjudicate(self._request()).decision, "abstain")

    def test_every_decision_carries_cited_evidence(self):
        outcome = self.adjudicator.adjudicate(self._request(dict(
            id="a", amount=100000, date="2026-07-15", delta=0,
            narration="NEFT-AXISP00123456789-RAZORPAY", reference_score=1.0,
            reference_mechanism="exact_substring", banking_days_late=0,
        )))
        self.assertTrue(outcome.evidence)
        for item in outcome.evidence:
            self.assertIn("setl_1", item.record_ids)


class TestHostedAdjudicatorContract(unittest.TestCase):
    """The hosted adapter's parsing rules, exercised without a network call."""

    def setUp(self):
        from finctl.adjudicate.claude import ClaudeAdjudicator

        self.cls = ClaudeAdjudicator

    def test_rejects_malformed_replies(self):
        for text in ("not json at all", "", "{unclosed", "[1, 2, 3]"):
            self.assertIsNone(self.cls._parse(text), text)

    def test_accepts_a_fenced_json_reply(self):
        parsed = self.cls._parse(
            '```json\n{"decision": "match", "candidate_id": "bank_1"}\n```'
        )
        self.assertEqual(parsed["candidate_id"], "bank_1")

    def test_missing_api_key_refuses_to_construct(self):
        import os

        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError):
                self.cls()
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
