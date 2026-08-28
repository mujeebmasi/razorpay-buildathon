"""Canonical domain model for three-way settlement reconciliation.

The three sources being reconciled:

  PSP   -- what the payment gateway says it collected and paid out
  BANK  -- what actually hit the current account
  LEDGER-- what the business's own books expected

Everything is frozen and hashable. Records are never mutated after ingest; the
engine produces new objects describing relationships between them. That makes
a run reproducible and lets any conclusion be traced to the exact inputs that
produced it, which is the difference between an audit trail and a guess.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping, Sequence


class Source(str, Enum):
    """Which system a record came from."""

    PSP = "psp"
    BANK = "bank"
    LEDGER = "ledger"


class Severity(str, Enum):
    """How much a human should care, ordered by how much money is at risk.

    CRITICAL means cash is unaccounted for. HIGH means the books will not close
    without a decision. MEDIUM is a real break with a known cause. LOW and INFO
    are recorded for completeness and need no action.
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


class Tier(str, Enum):
    """Which stage of the cascade resolved a record.

    Ordered from cheapest and most certain to most expensive and least. The
    distribution across tiers is itself a headline metric: a healthy run
    resolves the overwhelming majority at T0-T2 for zero marginal cost, and
    only the genuinely hard residual reaches the adjudicator.
    """

    T0_IDENTIFIER = "T0"      # exact identifier + exact amount
    T1_NARRATION = "T1"       # identifier recovered from free-text narration
    T2_AMOUNT_DATE = "T2"     # unique amount within the settlement window
    T3_TOLERANCE = "T3"       # amount within rounding tolerance
    T4_FEE_MODEL = "T4"       # gross recovered by inverting the fee schedule
    T5_SUBSET_SUM = "T5"      # one credit explained by a set of settlements
    T6_ASSIGNMENT = "T6"      # global optimal assignment over the remainder
    T7_ADJUDICATED = "T7"     # decided by the adjudicator, verifier-approved
    UNRESOLVED = "unresolved"


class ReasonCode(str, Enum):
    """Why a record ended up where it did.

    Every resolved match and every open exception carries one. This taxonomy is
    the honest exception list: an operator can filter by code, and each code
    maps to a specific next action and a specific person to ask.
    """

    # --- Resolutions -----------------------------------------------------
    UTR_EXACT = "utr_exact"
    UTR_IN_NARRATION = "utr_in_narration"
    AMOUNT_DATE_UNIQUE = "amount_date_unique"
    ROUNDING_TOLERANCE = "rounding_tolerance"
    FEE_MODEL_INVERTED = "fee_model_inverted"
    BATCH_SUBSET_SUM = "batch_subset_sum"
    SPLIT_SETTLEMENT = "split_settlement"
    OPTIMAL_ASSIGNMENT = "optimal_assignment"
    ADJUDICATED_MATCH = "adjudicated_match"
    REVERSAL_NETTED = "reversal_netted"

    # --- Breaks that need a human ---------------------------------------
    NO_CANDIDATE = "no_candidate"
    AMOUNT_MISMATCH = "amount_mismatch"
    AMBIGUOUS_CANDIDATES = "ambiguous_candidates"
    SCALE_ERROR_SUSPECTED = "scale_error_suspected"
    TRANSPOSITION_SUSPECTED = "transposition_suspected"
    DATE_OUT_OF_WINDOW = "date_out_of_window"
    DUPLICATE_IDENTIFIER = "duplicate_identifier"
    MISSING_BANK_CREDIT = "missing_bank_credit"
    UNEXPECTED_BANK_CREDIT = "unexpected_bank_credit"
    FEE_SCHEDULE_DEVIATION = "fee_schedule_deviation"
    UNLINKED_REFUND = "unlinked_refund"
    UNLINKED_CHARGEBACK = "unlinked_chargeback"
    CURRENCY_MISMATCH = "currency_mismatch"
    FX_UNVERIFIED = "fx_unverified"
    LEDGER_ENTRY_MISSING = "ledger_entry_missing"
    PARTIAL_SETTLEMENT_OPEN = "partial_settlement_open"
    ADJUDICATOR_ABSTAINED = "adjudicator_abstained"
    VERIFIER_REJECTED = "verifier_rejected"
    MALFORMED_RECORD = "malformed_record"


#: Operator-facing guidance per break code: what it means, what to do next, and
#: who owns the resolution. Carried into the exception register so the output is
#: actionable rather than merely descriptive.
REASON_GUIDANCE: Mapping[ReasonCode, tuple[Severity, str, str]] = {
    ReasonCode.NO_CANDIDATE: (
        Severity.HIGH,
        "No counterpart record exists in the matching window at any tolerance.",
        "Finance ops: confirm the settlement report covers this period; re-pull the bank feed.",
    ),
    ReasonCode.AMOUNT_MISMATCH: (
        Severity.HIGH,
        "A counterpart exists on the right date but the amount differs beyond tolerance.",
        "Finance ops: compare against the PSP dashboard; likely an undisclosed deduction.",
    ),
    ReasonCode.AMBIGUOUS_CANDIDATES: (
        Severity.MEDIUM,
        "Two or more counterparts fit equally well; no evidence distinguishes them.",
        "Finance ops: pick using the payer narration or ask the PSP for the UTR breakdown.",
    ),
    ReasonCode.SCALE_ERROR_SUSPECTED: (
        Severity.CRITICAL,
        "Amounts differ by exactly 100x -- a paise/rupee unit error in an integration.",
        "Engineering: audit the currency unit on the feed that produced this record.",
    ),
    ReasonCode.TRANSPOSITION_SUSPECTED: (
        Severity.HIGH,
        "Amounts are digit transpositions of one another -- consistent with manual keying.",
        "Finance ops: verify against source document before correcting either side.",
    ),
    ReasonCode.DATE_OUT_OF_WINDOW: (
        Severity.MEDIUM,
        "Amount and identifier agree but the date falls outside the contracted cycle.",
        "Finance ops: confirm the settlement SLA; a persistent drift is a contract issue.",
    ),
    ReasonCode.DUPLICATE_IDENTIFIER: (
        Severity.CRITICAL,
        "The same identifier appears on multiple records; one may be a double-post.",
        "Engineering: check the ingest pipeline for replay; do not settle until resolved.",
    ),
    ReasonCode.MISSING_BANK_CREDIT: (
        Severity.HIGH,
        "The PSP reports a payout with no corresponding bank credit.",
        "Treasury: if beyond the SLA, raise with the PSP; funds may be on hold.",
    ),
    ReasonCode.UNEXPECTED_BANK_CREDIT: (
        Severity.MEDIUM,
        "A bank credit that no settlement or ledger entry explains.",
        "Treasury: likely a direct customer transfer or non-PSP income; classify manually.",
    ),
    ReasonCode.FEE_SCHEDULE_DEVIATION: (
        Severity.HIGH,
        "The fee charged does not match the contracted rate card beyond tolerance.",
        "Finance: raise with the PSP account manager; recoverable overcharge.",
    ),
    ReasonCode.UNLINKED_REFUND: (
        Severity.MEDIUM,
        "A refund was netted off a payout but its original payment is not in scope.",
        "Finance ops: widen the period; the original payment likely predates this batch.",
    ),
    ReasonCode.UNLINKED_CHARGEBACK: (
        Severity.HIGH,
        "A chargeback deduction with no matching disputed payment in scope.",
        "Risk: pull the dispute record; deadline-sensitive for representment.",
    ),
    ReasonCode.CURRENCY_MISMATCH: (
        Severity.HIGH,
        "Records agree on identifier but disagree on currency.",
        "Engineering: a currency field is being dropped or defaulted somewhere.",
    ),
    ReasonCode.FX_UNVERIFIED: (
        Severity.MEDIUM,
        "A cross-currency settlement whose conversion rate cannot be independently checked.",
        "Treasury: obtain the applied rate from the PSP to verify the markup.",
    ),
    ReasonCode.LEDGER_ENTRY_MISSING: (
        Severity.MEDIUM,
        "Money was collected and settled but the internal books have no entry.",
        "Finance ops: create the receivable; revenue is currently unrecorded.",
    ),
    ReasonCode.PARTIAL_SETTLEMENT_OPEN: (
        Severity.LOW,
        "Part of this payment settled; the balance is still on hold at the PSP.",
        "No action -- expected to clear in a later cycle. Escalate if it ages.",
    ),
    ReasonCode.ADJUDICATOR_ABSTAINED: (
        Severity.MEDIUM,
        "The adjudicator saw the evidence and declined to decide rather than guess.",
        "Finance ops: a human judgement call; the candidate set is attached.",
    ),
    ReasonCode.VERIFIER_REJECTED: (
        Severity.HIGH,
        "The adjudicator proposed a match and the arithmetic verifier refused it.",
        "Engineering: review the rejected proposal; this is a guardrail firing correctly.",
    ),
    ReasonCode.MALFORMED_RECORD: (
        Severity.CRITICAL,
        "The record could not be parsed into the canonical model.",
        "Engineering: fix the source export; this record was excluded from all totals.",
    ),
}


def _stable_id(prefix: str, *parts: Any) -> str:
    """A short deterministic id derived from content.

    Deterministic so that re-running the same batch produces identical ids,
    which is what makes runs diffable and posting idempotent.
    """
    digest = hashlib.blake2b(
        "|".join(str(p) for p in parts).encode("utf-8"), digest_size=6
    ).hexdigest()
    return f"{prefix}_{digest}"


@dataclass(frozen=True, slots=True)
class Payment:
    """A single customer payment as the PSP reports it.

    `gross` is what the customer paid. `fee` and `tax` are the PSP's charges.
    `net` is what should reach the bank. The invariant net == gross - fee - tax
    is checked at ingest, not assumed.
    """

    payment_id: str
    order_id: str
    gross: int
    fee: int
    tax: int
    net: int
    captured_at: datetime
    method: str
    currency: str = "INR"
    settlement_id: str | None = None
    international: bool = False
    fx_rate: str | None = None          # Decimal-as-string; None for domestic
    source_currency: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def captured_on(self) -> date:
        return self.captured_at.date()

    @property
    def fee_bearing(self) -> int:
        """Total PSP deduction on this payment."""
        return self.fee + self.tax


@dataclass(frozen=True, slots=True)
class Refund:
    """A refund netted off a payout."""

    refund_id: str
    payment_id: str
    amount: int
    created_at: datetime
    settlement_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Adjustment:
    """A PSP-side adjustment: chargeback, dispute hold, reserve, or correction.

    `amount` is signed from the merchant's perspective -- negative reduces the
    payout. `kind` drives which reason code an unlinked adjustment produces.
    """

    adjustment_id: str
    kind: str                     # chargeback | reserve | correction | dispute_fee
    amount: int
    created_at: datetime
    settlement_id: str | None = None
    linked_payment_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Settlement:
    """A PSP payout batch: the thing that becomes one bank credit.

    `amount` is the net payout the PSP claims it sent. The engine's job is to
    prove that amount equals the sum of its components and that it arrived.
    """

    settlement_id: str
    utr: str | None
    amount: int
    settled_at: datetime
    payment_ids: tuple[str, ...] = ()
    refund_ids: tuple[str, ...] = ()
    adjustment_ids: tuple[str, ...] = ()
    currency: str = "INR"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def settled_on(self) -> date:
        return self.settled_at.date()


@dataclass(frozen=True, slots=True)
class BankLine:
    """One line on the bank statement.

    `amount` is signed: positive is a credit into the account. `narration` is
    the free-text field where the UTR is usually buried among bank-specific
    noise, and is the input to identifier recovery.
    """

    line_id: str
    value_date: date
    narration: str
    amount: int
    balance: int | None = None
    bank_ref: str | None = None
    currency: str = "INR"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_credit(self) -> bool:
        return self.amount > 0


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """What the business's own books expected to collect."""

    entry_id: str
    order_id: str
    expected_gross: int
    customer: str
    booked_on: date
    currency: str = "INR"
    status: str = "open"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Evidence:
    """One fact that supported or contradicted a decision.

    Every match carries its evidence. `weight` is signed: positive supports the
    match, negative counts against it. Nothing may claim a match without at
    least one piece of positive evidence naming the specific records involved.
    """

    kind: str
    detail: str
    weight: float
    record_ids: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"[{self.kind}] {self.detail}"


@dataclass(frozen=True, slots=True)
class Match:
    """A proven relationship between records across sources.

    Deliberately many-to-many: `bank_line_ids` and `settlement_ids` are both
    tuples because one credit can settle several batches and one batch can
    arrive as several credits. Modelling this as one-to-one is why most naive
    reconcilers fail on real data.
    """

    match_id: str
    bank_line_ids: tuple[str, ...]
    settlement_ids: tuple[str, ...]
    payment_ids: tuple[str, ...]
    ledger_entry_ids: tuple[str, ...]
    tier: Tier
    reason: ReasonCode
    confidence: float
    bank_total: int
    expected_total: int
    evidence: tuple[Evidence, ...]
    rationale: str = ""
    adjudicator: str | None = None

    @property
    def residual(self) -> int:
        """Unexplained paise. Must be within tolerance for the match to stand."""
        return self.bank_total - self.expected_total

    @classmethod
    def create(
        cls,
        *,
        bank_line_ids: Sequence[str],
        settlement_ids: Sequence[str],
        payment_ids: Sequence[str] = (),
        ledger_entry_ids: Sequence[str] = (),
        tier: Tier,
        reason: ReasonCode,
        confidence: float,
        bank_total: int,
        expected_total: int,
        evidence: Sequence[Evidence],
        rationale: str = "",
        adjudicator: str | None = None,
    ) -> "Match":
        bank_ids = tuple(sorted(bank_line_ids))
        settle_ids = tuple(sorted(settlement_ids))
        return cls(
            match_id=_stable_id("m", tier.value, *bank_ids, *settle_ids),
            bank_line_ids=bank_ids,
            settlement_ids=settle_ids,
            payment_ids=tuple(sorted(payment_ids)),
            ledger_entry_ids=tuple(sorted(ledger_entry_ids)),
            tier=tier,
            reason=reason,
            confidence=confidence,
            bank_total=bank_total,
            expected_total=expected_total,
            evidence=tuple(evidence),
            rationale=rationale,
            adjudicator=adjudicator,
        )


@dataclass(frozen=True, slots=True)
class Exception_:
    """An unresolved break, with everything a human needs to close it.

    Named with a trailing underscore to avoid shadowing the builtin. Carries
    the candidates that were considered and rejected, because "we looked and
    found nothing" and "we found three and could not choose" are different
    problems with different owners.
    """

    exception_id: str
    subject_source: Source
    subject_ids: tuple[str, ...]
    reason: ReasonCode
    severity: Severity
    amount: int
    as_of: date
    summary: str
    suggested_action: str
    owner: str
    evidence: tuple[Evidence, ...] = ()
    candidates: tuple[str, ...] = ()
    delta: int | None = None

    @classmethod
    def create(
        cls,
        *,
        subject_source: Source,
        subject_ids: Sequence[str],
        reason: ReasonCode,
        amount: int,
        as_of: date,
        evidence: Sequence[Evidence] = (),
        candidates: Sequence[str] = (),
        delta: int | None = None,
        summary_override: str | None = None,
        severity_override: Severity | None = None,
    ) -> "Exception_":
        severity, summary, action = REASON_GUIDANCE.get(
            reason,
            (Severity.MEDIUM, "Unclassified break.", "Finance ops: investigate manually."),
        )
        owner, _, action_text = action.partition(": ")
        ids = tuple(sorted(subject_ids))
        return cls(
            exception_id=_stable_id("x", reason.value, *ids),
            subject_source=subject_source,
            subject_ids=ids,
            reason=reason,
            severity=severity_override or severity,
            amount=amount,
            as_of=as_of,
            summary=summary_override or summary,
            suggested_action=action_text or action,
            owner=owner,
            evidence=tuple(evidence),
            candidates=tuple(candidates),
            delta=delta,
        )
