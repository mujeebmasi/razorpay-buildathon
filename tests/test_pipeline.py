"""End-to-end properties of a full run.

These are the tests that would catch a regression nobody predicted. They assert
the properties the system promises -- determinism, conservation of records,
accuracy floors, a balanced ledger -- rather than specific values, so they stay
meaningful as the engine changes.

The accuracy assertions are floors rather than exact figures on purpose. An
exact assertion on 99.8% would fail on any improvement, and a test that fails
when the code gets better trains people to ignore it.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from datagen.generate import Generator, write_world
from datagen.scenarios import CATALOGUE, Disposition
from finctl.adjudicate.offline import OfflineAdjudicator
from finctl.engine.reconcile import ReconConfig
from finctl.ingest.loader import load_batch, load_truth
from finctl.models import ReasonCode, Severity
from finctl.pipeline import run

DATA = Path(__file__).resolve().parent.parent / "data"


class TestFullRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run(DATA, adjudicator=OfflineAdjudicator())
        cls.card = cls.result.scorecard

    # -- the promises --------------------------------------------------

    def test_never_produces_a_false_match(self):
        self.assertEqual(
            self.card.false_match_rate, 0.0,
            f"false matches: {[c.detail for c in self.card.failures() if c.verdict.is_dangerous]}",
        )

    def test_match_precision_is_total(self):
        self.assertEqual(self.card.match_precision, 1.0)

    def test_accuracy_clears_the_floor(self):
        # A floor rather than a target. Measured accuracy sits in the mid
        # nineties and the shortfall is almost entirely multi-way batch
        # decomposition being refused as statistically unsafe -- a deliberate
        # trade of recall for the zero-false-match guarantee above.
        self.assertGreaterEqual(self.card.accuracy, 0.93)

    def test_catches_every_break(self):
        self.assertGreaterEqual(self.card.exception_recall, 0.99)

    def test_reason_codes_are_almost_always_right(self):
        self.assertGreaterEqual(self.card.reason_accuracy, 0.97)

    def test_recall_loss_is_confined_to_batch_decomposition(self):
        """The accuracy shortfall must stay where we decided to accept it.

        If some other scenario starts failing, that is a regression hiding
        behind an aggregate number that still clears its floor. Pinning the
        location of the loss is what makes the floor meaningful.
        """
        good = {"correct_match", "correct_exception", "correct_ignore"}
        leaking = {
            scenario: tally
            for scenario, tally in self.card.by_scenario.items()
            if scenario != "batched_credit"
            and sum(count for verdict, count in tally.items() if verdict not in good)
            > 0.15 * sum(tally.values())
        }
        self.assertEqual(leaking, {}, f"unexpected failures outside batching: {leaking}")

    def test_does_not_claim_to_resolve_the_unresolvable(self):
        # The catalogue caps what any correct engine can auto-resolve. Beating
        # that ceiling would mean matching cases that cannot be decided.
        ceiling = sum(
            s.weight for s in CATALOGUE if s.disposition is not Disposition.EXCEPTION
        ) / sum(s.weight for s in CATALOGUE)
        self.assertLessEqual(
            self.card.auto_resolve_rate, ceiling + 0.02,
            "auto-resolve rate exceeds what the data makes resolvable",
        )

    # -- structural invariants -----------------------------------------

    def test_the_ledger_balances(self):
        self.assertTrue(self.result.posting.balances)
        self.assertEqual(
            self.result.posting.total_debits, self.result.posting.total_credits
        )
        self.assertEqual(self.result.posting.unbalanced, [])

    def test_no_record_is_claimed_by_two_matches(self):
        seen_lines: set[str] = set()
        seen_settlements: set[str] = set()
        for match in self.result.matches:
            for line_id in match.bank_line_ids:
                self.assertNotIn(line_id, seen_lines, f"{line_id} claimed twice")
                seen_lines.add(line_id)
            for settlement_id in match.settlement_ids:
                self.assertNotIn(settlement_id, seen_settlements)
                seen_settlements.add(settlement_id)

    def test_every_settlement_is_either_matched_or_explained(self):
        """Nothing may simply disappear between input and output."""
        matched = {
            sid for match in self.result.matches for sid in match.settlement_ids
        }
        flagged = {
            sid for exception in self.result.exceptions
            for sid in list(exception.subject_ids) + list(exception.candidates)
        }
        unaccounted = [
            settlement.settlement_id for settlement in self.result.batch.settlements
            if settlement.settlement_id not in matched
            and settlement.settlement_id not in flagged
        ]
        self.assertEqual(unaccounted, [], f"payouts vanished: {unaccounted[:5]}")

    def test_every_match_carries_evidence_naming_real_records(self):
        known = (
            set(self.result.batch.index_settlements())
            | set(self.result.batch.index_bank_lines())
        )
        for match in self.result.matches:
            self.assertTrue(match.evidence, f"{match.match_id} has no evidence")
            cited = {rid for item in match.evidence for rid in item.record_ids}
            self.assertTrue(
                cited & known or match.reason is ReasonCode.REVERSAL_NETTED,
                f"{match.match_id} cites no real record",
            )

    def test_every_exception_names_an_owner_and_an_action(self):
        for exception in self.result.exceptions:
            self.assertTrue(exception.owner, exception.exception_id)
            self.assertTrue(exception.suggested_action, exception.exception_id)
            self.assertTrue(exception.summary, exception.exception_id)

    def test_quarantined_rows_are_reported_and_excluded(self):
        quarantined = len(self.result.batch.quarantined)
        reported = sum(
            1 for exception in self.result.exceptions
            if exception.reason is ReasonCode.MALFORMED_RECORD
        )
        self.assertEqual(quarantined, reported)

    # -- operational -----------------------------------------------------

    def test_throughput_is_respectable(self):
        self.assertGreater(
            self.result.throughput, 1000,
            f"only {self.result.throughput:,.0f} records/second",
        )

    def test_the_cheap_tiers_do_most_of_the_work(self):
        # If the expensive stages were carrying the load, the cascade ordering
        # would be broken even though the output still looked correct.
        tiers = self.result.recon.tier_counts
        cheap = tiers.get("T0", 0) + tiers.get("T1", 0) + tiers.get("T2", 0)
        self.assertGreater(cheap / max(len(self.result.matches), 1), 0.7)

    def test_adjudicator_sees_only_a_small_residual(self):
        # The bound is 20% rather than 15% because refusing unsafe batch
        # decompositions deliberately leaves those payouts open, and they flow
        # through to this stage. The cascade is still doing the overwhelming
        # majority of the work deterministically, which is the property under
        # test; the exact residual moves with the safety settings.
        adjudicated = self.result.recon.counters.get("adjudicated", 0)
        self.assertLess(
            adjudicated / max(len(self.result.batch.settlements), 1), 0.20,
            "too much reached the expensive stage; the cascade is leaking",
        )


class TestDeterminism(unittest.TestCase):
    def test_two_runs_of_the_same_data_agree_exactly(self):
        first = run(DATA, adjudicator=OfflineAdjudicator())
        second = run(DATA, adjudicator=OfflineAdjudicator())

        self.assertEqual(
            [m.match_id for m in first.matches], [m.match_id for m in second.matches]
        )
        self.assertEqual(
            [e.exception_id for e in first.exceptions],
            [e.exception_id for e in second.exceptions],
        )
        self.assertEqual(
            [e.entry_id for e in first.posting.entries],
            [e.entry_id for e in second.posting.entries],
        )

    def test_posting_is_idempotent(self):
        # Re-posting the same matches must produce identical entry ids, so a
        # rerun cannot double-post to the ledger.
        from finctl.post.journal import post_matches

        result = run(DATA, adjudicator=OfflineAdjudicator())
        again = post_matches(result.matches, result.batch)
        self.assertEqual(
            [e.entry_id for e in result.posting.entries],
            [e.entry_id for e in again.entries],
        )

    def test_the_generator_is_reproducible(self):
        first = Generator(seed=99, cases=40).generate()
        second = Generator(seed=99, cases=40).generate()
        self.assertEqual(
            [row["settlement_id"] for row in first.settlements],
            [row["settlement_id"] for row in second.settlements],
        )


class TestUnseenData(unittest.TestCase):
    """The engine must hold up on a batch it was never tuned against."""

    def test_holds_accuracy_on_a_different_seed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            write_world(Generator(seed=777, cases=350).generate(), path)
            result = run(path, adjudicator=OfflineAdjudicator())

            self.assertEqual(result.scorecard.false_match_rate, 0.0)
            self.assertGreaterEqual(result.scorecard.accuracy, 0.93)
            self.assertTrue(result.posting.balances)

    def test_handles_an_empty_directory_without_crashing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            result = run(Path(temporary), adjudicator=OfflineAdjudicator())
            self.assertEqual(result.matches, [])
            self.assertEqual(result.records, 0)
            self.assertTrue(result.posting.balances)

    def test_tightening_tolerance_never_increases_matches(self):
        # A stricter tolerance must be strictly more conservative. If it is
        # not, some pass is ignoring the setting.
        loose = run(DATA, config=ReconConfig(amount_tolerance=5),
                    adjudicator=OfflineAdjudicator())
        strict = run(DATA, config=ReconConfig(amount_tolerance=0),
                     adjudicator=OfflineAdjudicator())
        self.assertLessEqual(len(strict.matches), len(loose.matches))
        self.assertEqual(strict.scorecard.false_match_rate, 0.0)


if __name__ == "__main__":
    unittest.main()
