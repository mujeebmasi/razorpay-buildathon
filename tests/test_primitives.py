"""Money, time, fee model, narration, and decomposition.

These are the layers everything else assumes are correct, so they are tested
against the specific ways each one is known to fail rather than for coverage.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from finctl.engine.feemodel import (
    DEFAULT_RATE_CARD, fee_breakdown, invert_net_to_gross, net_from_gross, verify_fee,
)
from finctl.engine.narration import (
    bounded_edit_distance, extract_candidates, normalize_ref, score_reference_match,
)
from finctl.engine.subsetsum import Item, closest_subset, find_subsets
from finctl.money import (
    MoneyParseError, digit_transposition, format_inr, looks_like_scale_error,
    parse_money,
)
from finctl.timeutil import (
    DateParseError, add_banking_days, banking_days_between, business_date,
    is_banking_day, parse_datetime,
)


class TestMoneyParsing(unittest.TestCase):
    def test_indian_lakh_grouping(self):
        self.assertEqual(parse_money("12,34,567.89"), 123456789)
        self.assertEqual(format_inr(123456789, symbol=False), "12,34,567.89")

    def test_european_decimal_comma(self):
        # 1.234,56 is one thousand two hundred, not one point two three four.
        self.assertEqual(parse_money("1.234,56"), 123456)

    def test_us_and_indian_format_agree(self):
        self.assertEqual(parse_money("1,234.56"), parse_money("1234.56"))

    def test_accounting_negative(self):
        self.assertEqual(parse_money("(1,234.50)"), -123450)

    def test_debit_credit_markers(self):
        self.assertEqual(parse_money("500 DR"), -50000)
        self.assertEqual(parse_money("500 CR"), 50000)

    def test_currency_symbols_stripped(self):
        for text in ("₹1,234.50", "Rs. 1234.50", "INR 1234.50", "1234.50 INR"):
            self.assertEqual(parse_money(text), 123450, text)

    def test_null_placeholders_rejected_not_zeroed(self):
        # The single most dangerous behaviour would be returning 0 here.
        for bad in ("", "-", "N/A", "NULL", "#REF!", "abc", None):
            with self.assertRaises(MoneyParseError, msg=repr(bad)):
                parse_money(bad)

    def test_bool_is_not_money(self):
        with self.assertRaises(MoneyParseError):
            parse_money(True)

    def test_rounding_is_half_up_not_bankers(self):
        # Banker's rounding would give 2 here; the GST rules require 3.
        self.assertEqual(parse_money(Decimal("0.025")), 3)

    def test_no_float_drift_over_many_additions(self):
        total = sum(parse_money("0.10") for _ in range(1000))
        self.assertEqual(total, 10000)  # exactly Rs 100.00

    def test_scale_error_detection_survives_truncation(self):
        # A rupee value read as paise truncates, so an exact 100x test misses it.
        self.assertTrue(looks_like_scale_error(123456, 1234))
        self.assertFalse(looks_like_scale_error(123456, 123400))

    def test_transposition_detection(self):
        self.assertTrue(digit_transposition(123400, 124300))
        self.assertFalse(digit_transposition(123400, 123500))


class TestBankingTime(unittest.TestCase):
    def test_ist_midnight_boundary(self):
        # 18:30 UTC is exactly 00:00 IST the next day.
        self.assertEqual(business_date("2026-07-15T18:29:00Z"), date(2026, 7, 15))
        self.assertEqual(business_date("2026-07-15T18:30:00Z"), date(2026, 7, 16))

    def test_utc_timestamp_moves_to_next_ist_day(self):
        self.assertEqual(business_date("2026-07-15T23:45:00Z"), date(2026, 7, 16))

    def test_naive_timestamps_are_treated_as_ist(self):
        moment = parse_datetime("2026-07-15 10:00:00")
        self.assertEqual(moment.utcoffset().total_seconds(), 5.5 * 3600)

    def test_t_plus_two_skips_the_weekend(self):
        # Friday 10 July 2026 + 2 banking days is Tuesday the 14th.
        self.assertEqual(add_banking_days(date(2026, 7, 10), 2), date(2026, 7, 14))

    def test_settlement_slips_past_a_bank_holiday(self):
        self.assertFalse(is_banking_day(date(2026, 11, 8)))     # Diwali, a Sunday
        self.assertEqual(add_banking_days(date(2026, 11, 5), 2), date(2026, 11, 9))

    def test_banking_days_between_is_signed(self):
        friday, tuesday = date(2026, 7, 10), date(2026, 7, 14)
        self.assertEqual(banking_days_between(friday, tuesday), 2)
        self.assertEqual(banking_days_between(tuesday, friday), -2)

    def test_every_export_date_format_parses(self):
        for text in (
            "15/07/2026", "15-07-2026", "15-Jul-2026", "2026-07-15",
            "Jul 15, 2026", "15 Jul 2026", "2026-07-15T10:00:00+05:30",
        ):
            self.assertEqual(business_date(text), date(2026, 7, 15), text)

    def test_implausible_epoch_rejected(self):
        with self.assertRaises(DateParseError):
            parse_datetime(12345)  # an order number, not a timestamp


class TestFeeModel(unittest.TestCase):
    def test_gst_is_charged_on_commission(self):
        commission, gst = fee_breakdown(100000, "credit_card")
        self.assertEqual(commission, 2000)          # 2% of Rs 1,000
        self.assertEqual(gst, 360)                  # 18% of the commission

    def test_debit_card_tier_changes_above_threshold(self):
        below, _ = fee_breakdown(150000, "debit_card")   # 0.40%
        above, _ = fee_breakdown(500000, "debit_card")   # 0.90%
        self.assertEqual(below, 600)
        self.assertEqual(above, 4500)

    def test_zero_mdr_methods_are_free(self):
        self.assertEqual(fee_breakdown(500000, "upi"), (0, 0))

    def test_inversion_recovers_the_gross_across_the_range(self):
        misses = 0
        for method in ("credit_card", "netbanking", "wallet", "emi", "amex"):
            for gross in range(1000, 2_000_000, 7919):   # prime stride
                recovered = invert_net_to_gross(net_from_gross(gross, method), method)
                if gross not in recovered:
                    misses += 1
        self.assertEqual(misses, 0, "inversion lost a gross amount it should recover")

    def test_inversion_reports_ambiguity_rather_than_guessing(self):
        # Double rounding makes some nets reachable from several grosses. The
        # correct behaviour is to return all of them, never to pick one.
        found_ambiguous = any(
            len(invert_net_to_gross(net_from_gross(g, "credit_card"), "credit_card")) > 1
            for g in range(1000, 200000)
        )
        self.assertTrue(found_ambiguous)

    def test_overcharge_is_detected_and_quantified(self):
        commission, gst = fee_breakdown(100000, "credit_card")
        ok, delta, explanation = verify_fee(100000, commission + 500, gst, "credit_card")
        self.assertFalse(ok)
        self.assertEqual(delta, 500)
        self.assertIn("overcharged", explanation)

    def test_single_paise_rounding_disagreement_is_tolerated(self):
        commission, gst = fee_breakdown(99999, "netbanking")
        ok, _, _ = verify_fee(99999, commission, gst + 1, "netbanking")
        self.assertTrue(ok)


class TestNarration(unittest.TestCase):
    def test_recovers_reference_from_every_bank_dialect(self):
        cases = [
            ("NEFT-AXISP00123456789-RAZORPAY SOFTWARE PRIVATE LIMITED", "AXISP00123456789"),
            ("MMT/IMPS/512345678901/Settlement/RAZORPAY", "512345678901"),
            ("RTGS-HDFCR52026071500123456-RZPY PAYOUT", "HDFCR52026071500123456"),
            ("BY TRANSFER-NEFT*SBIN0000001*RZPX123456789012*RAZORPAY", "RZPX123456789012"),
        ]
        for narration, utr in cases:
            score, mechanism, _ = score_reference_match(utr, narration)
            self.assertEqual(score, 1.0, narration)
            self.assertIn("exact", mechanism)

    def test_truncated_narration_still_matches(self):
        score, mechanism, _ = score_reference_match(
            "ICIC002345678901", "ACH C- RAZORPAY SOFTWARE-ICIC00234567"
        )
        self.assertGreater(score, 0.6)
        self.assertEqual(mechanism, "truncated_prefix")

    def test_transposed_reference_still_matches(self):
        score, mechanism, _ = score_reference_match(
            "AXISP00123456789", "NEFT-AXISP00123465789-RAZORPAY"
        )
        self.assertGreaterEqual(score, 0.8)
        self.assertTrue(mechanism.startswith("edit_distance"))

    def test_unrelated_narration_scores_zero(self):
        score, _, _ = score_reference_match(
            "AXISP00123456789", "SALARY CREDIT ACME CORP 998877"
        )
        self.assertEqual(score, 0.0)

    def test_counterparty_names_are_not_mistaken_for_references(self):
        tokens = {c.token for c in extract_candidates(
            "NEFT RAZORPAY SOFTWARE PRIVATE LIMITED SETTLEMENT"
        )}
        self.assertEqual(tokens, set())

    def test_separators_do_not_affect_identity(self):
        self.assertEqual(normalize_ref("AXIS-P001 234"), normalize_ref("axisp001234"))

    def test_edit_distance_treats_transposition_as_one_edit(self):
        self.assertEqual(bounded_edit_distance("ABCD", "ABDC", 2), 1)

    def test_edit_distance_abandons_beyond_the_bound(self):
        self.assertEqual(bounded_edit_distance("AAAAAAA", "ZZZZZZZ", 2), 3)


class TestSubsetSum(unittest.TestCase):
    def _pool(self, count: int, seed: int = 11):
        import random

        rng = random.Random(seed)
        return [Item(f"s{i:03d}", rng.randrange(5000, 90_000_00)) for i in range(count)]

    def test_recovers_the_exact_component_set(self):
        import random

        pool = self._pool(30)
        truth = random.Random(3).sample(pool, 4)
        target = sum(item.amount for item in truth)
        outcome = find_subsets(pool, target)
        self.assertTrue(outcome.is_unique)
        self.assertEqual(outcome.solutions[0].refs, tuple(sorted(i.ref for i in truth)))

    def test_ambiguity_is_reported_not_resolved(self):
        # Two items of equal value make 15000 reachable three ways.
        pool = [Item("a", 10000), Item("b", 10000), Item("c", 5000), Item("d", 15000)]
        outcome = find_subsets(pool, 15000)
        self.assertFalse(outcome.is_unique)
        self.assertGreaterEqual(len(outcome.solutions), 3)

    def test_tolerance_absorbs_rounding_drift(self):
        pool = self._pool(20)
        target = sum(item.amount for item in pool[:3]) + 2
        outcome = find_subsets(pool, target, tolerance=3)
        self.assertTrue(outcome.solutions)
        self.assertEqual(abs(outcome.solutions[0].residual), 2)

    def test_branch_and_bound_matches_meet_in_the_middle(self):
        # The two strategies must agree; the pool size only chooses which runs.
        import random

        pool = self._pool(40, seed=7)
        truth = random.Random(5).sample(pool, 3)
        target = sum(item.amount for item in truth)
        small = find_subsets(pool, target, max_pool=100)      # meet in the middle
        large = find_subsets(pool, target, max_pool=10)       # branch and bound
        self.assertEqual(
            {s.refs for s in small.solutions}, {s.refs for s in large.solutions}
        )

    def test_large_pool_completes_quickly(self):
        import time

        pool = self._pool(120, seed=13)
        target = sum(item.amount for item in pool[:5])
        started = time.perf_counter()
        outcome = find_subsets(pool, target)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertTrue(outcome.solutions)

    def test_closest_subset_gives_a_lead_when_nothing_fits(self):
        pool = self._pool(20)
        nearest = closest_subset(pool, sum(i.amount for i in pool[:3]) + 999_00)
        self.assertIsNotNone(nearest)
        self.assertLess(abs(nearest.residual), 999_00)

    def test_empty_set_is_never_a_solution(self):
        outcome = find_subsets([Item("a", 100)], 0)
        self.assertEqual(outcome.solutions, ())


if __name__ == "__main__":
    unittest.main()


class TestDecompositionCredibility(unittest.TestCase):
    """The rules that stop a coincidental sum being reported as a fact."""

    def test_cardinality_shrinks_as_the_pool_grows(self):
        from finctl.engine.subsetsum import credible_cardinality

        # Six payouts out of ten is a claim worth making; six out of two
        # hundred is arithmetic coincidence with a confident label on it.
        self.assertEqual(credible_cardinality(10, 6), 6)
        self.assertGreater(credible_cardinality(20, 6), credible_cardinality(60, 6))
        self.assertLessEqual(credible_cardinality(250, 6), 2)

    def test_a_dense_neighbourhood_is_reported_as_incredible(self):
        from finctl.engine.subsetsum import Item, find_subsets
        import random

        # Many similar small amounts make almost any target reachable.
        rng = random.Random(5)
        pool = [Item(f"s{i}", rng.randrange(100_00, 110_00)) for i in range(40)]
        target = sum(item.amount for item in pool[:4])
        outcome = find_subsets(pool, target, tolerance=5, max_cardinality=4)
        self.assertFalse(
            outcome.is_credible,
            "a sum found among near-identical amounts must not count as evidence",
        )

    def test_a_sparse_neighbourhood_is_credible(self):
        from finctl.engine.subsetsum import Item, find_subsets

        # Widely separated amounts: hitting the target exactly means something.
        pool = [Item(f"s{i}", (i + 1) * 7_000_000) for i in range(8)]
        target = pool[0].amount + pool[3].amount
        outcome = find_subsets(pool, target, tolerance=5, max_cardinality=4)
        self.assertTrue(outcome.solutions)
        self.assertTrue(outcome.is_credible)
