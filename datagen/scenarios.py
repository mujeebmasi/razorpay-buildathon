"""The edge-case catalogue.

Each scenario is a named, deliberately-constructed situation that a settlement
reconciler has to survive, together with the outcome a correct engine should
produce. Generating data from a catalogue -- rather than sprinkling random
noise -- is what makes the accuracy number mean something: every record has a
known right answer, including the records whose right answer is "this cannot be
resolved automatically".

That last category is the important one. A generator that only produces
solvable cases measures nothing, because an engine that matches everything
scores 100%. Roughly a fifth of the catalogue is unresolvable by construction,
and an engine that claims to have resolved those is wrong in the way that
actually costs money.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Disposition(str, Enum):
    """What should happen to a record in a correct run."""

    MATCH = "match"            # resolves to a specific counterpart set
    EXCEPTION = "exception"    # correctly ends up on the exception register
    IGNORE = "ignore"          # informational; must not be counted as a break


@dataclass(frozen=True, slots=True)
class Scenario:
    """One catalogue entry.

    `weight` is the relative frequency in a generated batch, chosen to mirror a
    real merchant's mix: mostly clean, with a long tail of the awkward. Setting
    every scenario to equal weight would produce a batch that is 4% clean,
    which no engine would ever see and which would make throughput numbers
    meaningless.

    `expected_reason` is the reason code a correct engine must emit. Scoring
    against it is stricter than scoring match/no-match alone: an engine that
    flags the right record for the wrong reason has not actually understood it.
    """

    key: str
    title: str
    description: str
    disposition: Disposition
    expected_reason: str
    weight: int
    difficulty: str            # trivial | routine | hard | unresolvable


CATALOGUE: tuple[Scenario, ...] = (
    # ---- The easy majority ------------------------------------------------
    Scenario(
        "clean_exact", "Clean settlement",
        "UTR present verbatim in the narration, amount exact, lands inside the T+2 window.",
        Disposition.MATCH, "utr_exact", 300, "trivial",
    ),
    Scenario(
        "utr_noisy_narration", "UTR buried in narration",
        "The UTR is present but wrapped in bank-specific rail markers, counterparty "
        "names and a trailing balance.",
        Disposition.MATCH, "utr_exact", 120, "trivial",
    ),

    # ---- Identifier damage ------------------------------------------------
    Scenario(
        "utr_truncated", "Narration clipped at field width",
        "The bank export truncates narration at 40 characters, cutting the UTR short. "
        "Only a prefix survives.",
        Disposition.MATCH, "utr_in_narration", 45, "routine",
    ),
    Scenario(
        "utr_typo", "Corrupted UTR",
        "Two adjacent characters of the UTR are transposed, as happens when a "
        "reference is re-keyed by hand into a payment portal.",
        Disposition.MATCH, "utr_in_narration", 25, "hard",
    ),
    Scenario(
        "no_utr_unique_amount", "No reference at all",
        "The narration carries no recoverable reference. The amount and date are "
        "unique within the window, so it is still resolvable.",
        Disposition.MATCH, "amount_date_unique", 55, "routine",
    ),

    # ---- Arithmetic edges -------------------------------------------------
    Scenario(
        "rounding_drift", "Paise-level rounding drift",
        "The bank credit differs from the settlement by one or two paise, from the "
        "PSP and the bank rounding a fee split differently.",
        Disposition.MATCH, "rounding_tolerance", 40, "routine",
    ),
    Scenario(
        "fee_recovered", "Gross recovered from net",
        "Only the net credit is known. The gross is recovered by inverting the "
        "contracted rate card for the payment method.",
        Disposition.MATCH, "fee_model_inverted", 30, "hard",
    ),

    # ---- Batching ---------------------------------------------------------
    Scenario(
        "batched_credit", "Several payouts, one credit",
        "The PSP nets three to six settlements into a single bank transfer. The "
        "component set has to be recovered by decomposition.",
        Disposition.MATCH, "batch_subset_sum", 70, "hard",
    ),
    Scenario(
        "split_settlement", "One payout, several credits",
        "A large settlement arrives as two bank credits on the same or adjacent "
        "days, typically because it crossed an RTGS batch limit.",
        Disposition.MATCH, "split_settlement", 30, "hard",
    ),

    # ---- Timing -----------------------------------------------------------
    Scenario(
        "late_credit", "Late but within slack",
        "The credit lands a day later than the contracted cycle, inside the "
        "tolerance the window allows.",
        Disposition.MATCH, "utr_exact", 35, "routine",
    ),
    Scenario(
        "holiday_slip", "Settlement across a bank holiday",
        "T+2 spans Diwali, so the credit lands two calendar days later than a naive "
        "calendar window would predict.",
        Disposition.MATCH, "utr_exact", 20, "routine",
    ),
    Scenario(
        "timezone_boundary", "Capture just before IST midnight",
        "Captured at 23:45 UTC, which is the following day in IST. Bucketing on the "
        "UTC date puts it in the wrong batch entirely.",
        Disposition.MATCH, "utr_exact", 25, "hard",
    ),
    Scenario(
        "very_late_credit", "Beyond the settlement SLA",
        "Identifier and amount agree, but the credit is far outside the contracted "
        "cycle. Matching it silently would hide an SLA breach.",
        Disposition.EXCEPTION, "date_out_of_window", 18, "routine",
    ),

    # ---- Deductions -------------------------------------------------------
    Scenario(
        "refund_netted", "Refund netted off the payout",
        "A refund issued during the cycle is deducted from the payout, so the credit "
        "is smaller than the sum of its payments. The payout must still reconcile "
        "once the refund is taken into account.",
        Disposition.MATCH, "utr_exact", 45, "routine",
    ),
    Scenario(
        "chargeback_deduction", "Chargeback deducted",
        "A disputed payment is clawed back mid-cycle, reducing the payout by the "
        "disputed amount plus a dispute fee. Both deductions must be accounted for.",
        Disposition.MATCH, "utr_exact", 25, "hard",
    ),
    Scenario(
        "unlinked_chargeback", "Chargeback for an out-of-period payment",
        "A deduction appears for a payment captured before the period under review, "
        "so nothing in scope explains it.",
        Disposition.EXCEPTION, "unlinked_chargeback", 12, "unresolvable",
    ),
    Scenario(
        "unlinked_refund", "Refund for an out-of-period payment",
        "A refund nets off a payout but its original payment predates the batch.",
        Disposition.EXCEPTION, "unlinked_refund", 12, "unresolvable",
    ),

    # ---- Genuine breaks ---------------------------------------------------
    Scenario(
        "missing_bank_credit", "Payout never arrived",
        "The PSP reports a settlement that has no corresponding bank credit. Either "
        "the feed lags or the money is on hold.",
        Disposition.EXCEPTION, "missing_bank_credit", 22, "unresolvable",
    ),
    Scenario(
        "unexpected_credit", "Credit from outside the PSP",
        "A direct customer NEFT or an inter-account sweep that no settlement "
        "explains. Not an error, but it must not be force-matched.",
        Disposition.EXCEPTION, "unexpected_bank_credit", 20, "unresolvable",
    ),
    Scenario(
        "amount_mismatch", "Undisclosed deduction",
        "Identifier and date agree but the credit is materially short, with no "
        "documented refund or adjustment to account for it.",
        Disposition.EXCEPTION, "amount_mismatch", 18, "routine",
    ),
    Scenario(
        "same_amount_ambiguity", "Two identical payouts, no reference",
        "Two settlements of exactly the same amount on the same day, neither "
        "carrying a recoverable UTR. No evidence distinguishes them.",
        Disposition.EXCEPTION, "ambiguous_candidates", 16, "unresolvable",
    ),
    Scenario(
        "scale_error", "Paise/rupee unit slip",
        "One side reports the amount 100x off -- a currency-unit bug between the PSP "
        "API and the accounting export.",
        Disposition.EXCEPTION, "scale_error_suspected", 10, "routine",
    ),
    Scenario(
        "transposition", "Transposed digits",
        "The amount has two digits swapped relative to its counterpart, the "
        "signature of manual entry.",
        Disposition.EXCEPTION, "transposition_suspected", 10, "routine",
    ),
    Scenario(
        "duplicate_utr", "Same UTR on two credits",
        "One UTR appears on two bank lines. One of them is a double-post, and "
        "matching both would overstate cash.",
        Disposition.EXCEPTION, "duplicate_identifier", 10, "hard",
    ),
    Scenario(
        "fee_overcharge", "Fee above the rate card",
        "The PSP charged more commission than the negotiated rate allows. "
        "Recoverable money that a match-only reconciler never notices.",
        Disposition.EXCEPTION, "fee_schedule_deviation", 14, "hard",
    ),
    Scenario(
        "fx_unverified", "International settlement",
        "A USD payment settled in INR at a rate that cannot be independently "
        "verified from the data supplied.",
        Disposition.EXCEPTION, "fx_unverified", 12, "unresolvable",
    ),
    Scenario(
        "ledger_missing", "Collected but not booked",
        "Money was collected and settled, but the internal ledger has no entry. "
        "Revenue is currently unrecorded.",
        Disposition.EXCEPTION, "ledger_entry_missing", 14, "routine",
    ),
    Scenario(
        "partial_on_hold", "Partial settlement",
        "Part of the payment settled and the balance is retained as a reserve, to "
        "be released in a later cycle.",
        Disposition.EXCEPTION, "partial_settlement_open", 12, "routine",
    ),
    Scenario(
        "malformed_amount", "Unparseable field",
        "A corrupted amount field in the export. The record must be quarantined and "
        "excluded from totals, never coerced to zero.",
        Disposition.EXCEPTION, "malformed_record", 8, "routine",
    ),

    # ---- Must not be treated as a break -----------------------------------
    Scenario(
        "reversal_pair", "Credit reversed the same day",
        "A credit and an identical debit that cancel out -- a failed transfer that "
        "was returned. Both lines must net to nothing, not become two breaks.",
        Disposition.IGNORE, "reversal_netted", 18, "hard",
    ),
)

BY_KEY: dict[str, Scenario] = {s.key: s for s in CATALOGUE}

TOTAL_WEIGHT: int = sum(s.weight for s in CATALOGUE)


def resolvable_share() -> float:
    """Fraction of the catalogue weight that a correct engine can auto-resolve.

    This is the theoretical ceiling on match rate. Reporting a run's match rate
    without it is misleading, because the remainder is unresolvable by
    construction and no engine should reach 100%.
    """
    resolvable = sum(
        s.weight for s in CATALOGUE if s.disposition is not Disposition.EXCEPTION
    )
    return resolvable / TOTAL_WEIGHT
