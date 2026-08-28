"""The verifier: an independent check on everything the engine concluded.

This module is the reason the system can be trusted with a reasoning component.
It re-derives every match from the original records and rejects any that fails
an invariant -- **including matches produced by the adjudicator**. The
adjudicator proposes; it does not decide. If its proposal does not balance, the
verifier throws it out and the record goes back on the exception register with
its reasoning attached for a human to read.

That asymmetry is deliberate. A reasoning engine, local or hosted, is a
plausibility machine: it is very good at producing an answer that looks right.
Arithmetic is not a plausibility machine. Putting the arithmetic downstream of
the reasoning, with the power to veto, means a confident wrong answer cannot
reach the ledger no matter how convincing its rationale.

The verifier deliberately shares no code with the matching passes. It recomputes
totals from the raw records rather than trusting the figures a Match carries,
because a check that reuses the calculation it is checking is not a check.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

from finctl.ingest.loader import Batch
from finctl.models import Evidence, Exception_, Match, ReasonCode, Source, Tier
from finctl.money import format_inr
from finctl.timeutil import banking_days_between


class Invariant(str, Enum):
    """The properties every accepted match must satisfy."""

    BALANCES = "balances"                    # the two sides agree within tolerance
    NO_DOUBLE_CLAIM = "no_double_claim"      # no record used by two matches
    RECORDS_EXIST = "records_exist"          # every referenced id is real
    HAS_EVIDENCE = "has_evidence"            # nothing matched without a reason
    CREDITS_ONLY = "credits_only"            # settlements match credits, not debits
    CONFIDENCE_SANE = "confidence_sane"      # confidence is a probability
    DATE_PLAUSIBLE = "date_plausible"        # the credit landed near the payout


@dataclass(frozen=True, slots=True)
class Violation:
    """One invariant failure, attributed to the match that caused it."""

    match_id: str
    invariant: Invariant
    detail: str
    tier: str
    adjudicated: bool

    def __str__(self) -> str:
        return f"{self.match_id} [{self.invariant.value}] {self.detail}"


@dataclass(slots=True)
class VerificationReport:
    """What survived verification, and what did not."""

    accepted: list[Match] = field(default_factory=list)
    rejected: list[Match] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    checks_run: int = 0

    @property
    def rejection_count(self) -> int:
        return len(self.rejected)

    @property
    def adjudicated_rejections(self) -> int:
        """Rejections of reasoning-layer proposals -- the guardrail firing."""
        return sum(1 for violation in self.violations if violation.adjudicated)

    def violations_by_invariant(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for violation in self.violations:
            counts[violation.invariant.value] += 1
        return dict(counts)


class Verifier:
    """Re-checks matches against the source records that produced them."""

    def __init__(
        self,
        batch: Batch,
        *,
        amount_tolerance: int = 5,
        max_banking_days: int = 6,
    ) -> None:
        self.batch = batch
        self.amount_tolerance = amount_tolerance
        self.max_banking_days = max_banking_days
        self.settlements = batch.index_settlements()
        self.bank_lines = batch.index_bank_lines()
        self.payments = batch.index_payments()

    def _check(self, match: Match) -> list[Violation]:
        """Every invariant failure for one match. Returns empty when clean."""
        failures: list[Violation] = []
        adjudicated = match.tier is Tier.T7_ADJUDICATED

        def fail(invariant: Invariant, detail: str) -> None:
            failures.append(
                Violation(match.match_id, invariant, detail, match.tier.value, adjudicated)
            )

        # -- referenced records must exist ------------------------------
        missing_lines = [
            line_id for line_id in match.bank_line_ids if line_id not in self.bank_lines
        ]
        missing_settlements = [
            sid for sid in match.settlement_ids if sid not in self.settlements
        ]
        if missing_lines or missing_settlements:
            fail(
                Invariant.RECORDS_EXIST,
                f"references records not present in the batch: "
                f"{missing_lines + missing_settlements}",
            )
            # Without the records there is nothing further to verify.
            return failures

        lines = [self.bank_lines[line_id] for line_id in match.bank_line_ids]
        settlements = [self.settlements[sid] for sid in match.settlement_ids]

        # -- evidence ----------------------------------------------------
        if not match.evidence:
            fail(Invariant.HAS_EVIDENCE, "no evidence recorded")
        elif not any(item.weight > 0 for item in match.evidence):
            fail(
                Invariant.HAS_EVIDENCE,
                "evidence recorded but none of it supports the match",
            )

        # -- confidence --------------------------------------------------
        if not 0.0 <= match.confidence <= 1.0:
            fail(
                Invariant.CONFIDENCE_SANE,
                f"confidence {match.confidence} is outside [0, 1]",
            )

        # A reversal is explained by its own offsetting pair; it has no
        # settlement side and the remaining checks do not apply to it.
        if match.reason is ReasonCode.REVERSAL_NETTED:
            total = sum(line.amount for line in lines)
            if total != 0:
                fail(
                    Invariant.BALANCES,
                    f"reversal pair nets to {format_inr(total)} rather than zero",
                )
            return failures

        # -- arithmetic, recomputed from source --------------------------
        # Note the totals are recomputed rather than read off the Match. This
        # is the check that catches a fabricated match, so it must not trust
        # any figure the match carries about itself.
        bank_total = sum(line.amount for line in lines)
        expected_total = sum(settlement.amount for settlement in settlements)
        residual = bank_total - expected_total

        if abs(residual) > self.amount_tolerance:
            fail(
                Invariant.BALANCES,
                f"bank side totals {format_inr(bank_total)} against a payout side of "
                f"{format_inr(expected_total)}, leaving {format_inr(abs(residual))} "
                f"unexplained -- beyond the {self.amount_tolerance} paise tolerance",
            )

        # The Match's own stated totals must agree with the recomputation. A
        # divergence means the match was constructed from different numbers
        # than the ones on file.
        if match.bank_total != bank_total or match.expected_total != expected_total:
            fail(
                Invariant.BALANCES,
                f"stated totals ({match.bank_total}/{match.expected_total}) do not "
                f"match the recomputed figures ({bank_total}/{expected_total})",
            )

        # -- direction ---------------------------------------------------
        for line in lines:
            if line.amount <= 0:
                fail(
                    Invariant.CREDITS_ONLY,
                    f"line {line.line_id} is a debit of {format_inr(line.amount)}; a "
                    f"payout must arrive as a credit",
                )

        # -- timing ------------------------------------------------------
        for settlement in settlements:
            for line in lines:
                gap = banking_days_between(settlement.settled_on, line.value_date)
                if abs(gap) > self.max_banking_days:
                    fail(
                        Invariant.DATE_PLAUSIBLE,
                        f"credit on {line.value_date} is {abs(gap)} banking days from "
                        f"the payout dated {settlement.settled_on}, beyond the "
                        f"{self.max_banking_days} day bound",
                    )

        return failures

    def verify(self, matches: Sequence[Match]) -> VerificationReport:
        """Check every match, then check the set of them for double-claiming."""
        report = VerificationReport()

        for match in matches:
            report.checks_run += 1
            failures = self._check(match)
            if failures:
                report.violations.extend(failures)
                report.rejected.append(match)
            else:
                report.accepted.append(match)

        # Double-claiming is a property of the accepted set rather than of any
        # single match, so it is checked once everything else has passed.
        # Checking it earlier would let a match that is about to be rejected
        # for other reasons "steal" a record from a valid one.
        claimed_lines: dict[str, str] = {}
        claimed_settlements: dict[str, str] = {}
        conflicted: set[str] = set()

        for match in report.accepted:
            for line_id in match.bank_line_ids:
                if (owner := claimed_lines.get(line_id)) and owner != match.match_id:
                    report.violations.append(
                        Violation(
                            match.match_id, Invariant.NO_DOUBLE_CLAIM,
                            f"bank line {line_id} is already claimed by {owner}",
                            match.tier.value, match.tier is Tier.T7_ADJUDICATED,
                        )
                    )
                    conflicted.add(match.match_id)
                else:
                    claimed_lines[line_id] = match.match_id
            for settlement_id in match.settlement_ids:
                if (owner := claimed_settlements.get(settlement_id)) and owner != match.match_id:
                    report.violations.append(
                        Violation(
                            match.match_id, Invariant.NO_DOUBLE_CLAIM,
                            f"payout {settlement_id} is already claimed by {owner}",
                            match.tier.value, match.tier is Tier.T7_ADJUDICATED,
                        )
                    )
                    conflicted.add(match.match_id)
                else:
                    claimed_settlements[settlement_id] = match.match_id

        if conflicted:
            still_good = [m for m in report.accepted if m.match_id not in conflicted]
            report.rejected.extend(
                m for m in report.accepted if m.match_id in conflicted
            )
            report.accepted = still_good

        return report


def rejections_to_exceptions(report: VerificationReport) -> list[Exception_]:
    """Turn every rejected match into a break a human can act on.

    A rejected match must not simply vanish. The record is still unreconciled,
    and the fact that something proposed a match for it -- and what that
    proposal was -- is exactly the context whoever picks it up needs.
    """
    by_match = defaultdict(list)
    for violation in report.violations:
        by_match[violation.match_id].append(violation)

    exceptions: list[Exception_] = []
    for match in report.rejected:
        failures = by_match.get(match.match_id, [])
        evidence = [
            Evidence(
                "invariant_violation", f"{v.invariant.value}: {v.detail}", -1.0,
                tuple(match.settlement_ids) + tuple(match.bank_line_ids),
            )
            for v in failures
        ]
        if match.rationale:
            evidence.append(
                Evidence(
                    "rejected_rationale",
                    f"the proposal claimed: {match.rationale}",
                    0.0,
                    tuple(match.settlement_ids),
                )
            )

        subject_ids = list(match.settlement_ids) or list(match.bank_line_ids)
        exceptions.append(
            Exception_.create(
                subject_source=Source.PSP if match.settlement_ids else Source.BANK,
                subject_ids=subject_ids,
                reason=ReasonCode.VERIFIER_REJECTED,
                amount=abs(match.expected_total or match.bank_total),
                as_of=_as_of(match, report),
                candidates=list(match.bank_line_ids),
                delta=match.residual,
                evidence=evidence,
                summary_override=(
                    f"A {match.tier.value} proposal"
                    + (f" from {match.adjudicator}" if match.adjudicator else "")
                    + f" was rejected by the verifier: "
                    + "; ".join(v.detail for v in failures[:2])
                ),
            )
        )
    return exceptions


def _as_of(match: Match, report: VerificationReport) -> "object":
    """Best available date for a rejected match, for register sorting."""
    from datetime import date as _date

    return _date.today()
