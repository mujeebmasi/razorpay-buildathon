"""The reconciliation cascade.

The controlling idea: **do the cheap, certain work first, and let the expensive
machinery see only what survives.** Passes run in descending order of evidential
strength, and each one removes what it resolves from the pool the next pass
considers. A reference printed verbatim in the narration is decisive, so that
pass runs first and costs nothing. Global assignment and adjudication are
powerful and expensive, so by the time they run there is very little left.

This ordering is not only a performance argument. Running a weak matcher before
a strong one lets it claim records the strong one would have matched correctly,
and those false matches are invisible -- the run looks *better*, because the
match rate goes up. Strongest-first is what makes the accuracy number real.

The second controlling idea: **every pass may decline.** Ambiguity is a result,
not a failure. A pass that finds two equally good candidates records an
ambiguity exception rather than picking one, because a wrong match costs an
operator more time than no match at all -- they have to discover it first.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Protocol, Sequence

from finctl.engine.feemodel import DEFAULT_RATE_CARD, invert_across_methods, verify_fee
from finctl.engine.index import BankIndex, SettlementIndex
from finctl.engine.narration import normalize_ref, score_reference_match
from finctl.engine.subsetsum import (
    Item, closest_subset, credible_cardinality, find_subsets,
)
from finctl.ingest.loader import Batch
from finctl.models import (
    Evidence, Exception_, Match, ReasonCode, Settlement, Severity, Source, Tier,
)
from finctl.money import digit_transposition, format_inr, looks_like_scale_error
from finctl.timeutil import add_banking_days, banking_days_between


@dataclass(frozen=True, slots=True)
class ReconConfig:
    """Tunable policy. Every value here is a business decision, not a magic number.

    `amount_tolerance` is the only place the system is permitted to call two
    different numbers equal. Five paise absorbs the rounding disagreement
    between a PSP and a bank splitting a fee; anything larger starts absorbing
    real shortfalls, which is precisely the failure this system exists to
    prevent.

    `identifier_max_banking_days` is the outer bound past which a correct
    reference stops being enough. A credit carrying the right UTR nine days
    late is still the right money, but matching it silently would bury an SLA
    breach, so beyond this bound it becomes an exception instead.
    """

    sla_days: int = 2
    window_slack: int = 1
    identifier_max_banking_days: int = 5
    amount_tolerance: int = 5
    batch_tolerance: int = 5
    max_batch_cardinality: int = 6
    narration_threshold: float = 0.60
    fee_tolerance: int = 1
    enable_adjudicator: bool = True
    #: How many candidates the adjudicator is shown. Beyond a handful nothing
    #: can be decided confidently anyway, and building the evidence for each
    #: one is the most expensive work in the cascade.
    adjudicator_candidates: int = 8


class Adjudicator(Protocol):
    """Anything that can decide the residual the deterministic passes left.

    Kept as a protocol so the reasoning engine is swappable -- a local solver, a
    hosted model, or a human queue -- without the cascade knowing which it got.
    """

    name: str

    def adjudicate(self, request: "AdjudicationRequest") -> "AdjudicationResult":
        ...


@dataclass(frozen=True, slots=True)
class AdjudicationRequest:
    """One unresolved record, with every candidate and its measured evidence."""

    subject_kind: str                  # "settlement" | "bank_line"
    subject_id: str
    subject_amount: int
    subject_date: date
    subject_description: str
    candidates: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class AdjudicationResult:
    """What the adjudicator concluded.

    `decision` is one of match / abstain. Abstaining is a first-class outcome
    and carries no penalty in the design: an adjudicator that abstains on
    genuinely ambiguous evidence is behaving correctly.
    """

    decision: str
    chosen_ids: tuple[str, ...]
    confidence: float
    rationale: str
    evidence: tuple[Evidence, ...] = ()


@dataclass(slots=True)
class ReconResult:
    """Everything a run produced, including how long each stage took."""

    matches: list[Match] = field(default_factory=list)
    exceptions: list[Exception_] = field(default_factory=list)
    stage_timings: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    adjudicator_name: str = "none"
    total_seconds: float = 0.0
    records_considered: int = 0

    @property
    def tier_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for match in self.matches:
            counts[match.tier.value] += 1
        return dict(counts)

    @property
    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for exception in self.exceptions:
            counts[exception.reason.value] += 1
        return dict(counts)


class Reconciler:
    """Runs the cascade over one ingested batch."""

    def __init__(
        self,
        batch: Batch,
        config: ReconConfig | None = None,
        adjudicator: Adjudicator | None = None,
    ) -> None:
        self.batch = batch
        self.config = config or ReconConfig()
        self.adjudicator = adjudicator
        self.result = ReconResult()

        self.bank_index = BankIndex.build(batch.bank_lines)
        self.settlement_index = SettlementIndex.build(batch.settlements)
        self.payments = batch.index_payments()
        self.refunds_by_id = {r.refund_id: r for r in batch.refunds}
        self.adjustments_by_id = {a.adjustment_id: a for a in batch.adjustments}
        self.ledger_by_order = {entry.order_id: entry for entry in batch.ledger}

        # Working pools. A record leaves its pool the moment it is explained,
        # so no later pass can claim it a second time. This is what enforces
        # the "each record used at most once" invariant structurally rather
        # than by checking for it afterwards.
        self.open_settlements: set[str] = set(self.settlement_index.by_id)
        self.open_credits: set[str] = set(self.bank_index.credit_ids)
        self.open_debits: set[str] = set(self.bank_index.debit_ids)

        # Reasoning from adjudications that declined to decide, keyed by
        # subject. Carried forward so the eventual exception can show what was
        # considered and why it was not enough.
        self.abstentions: dict[str, tuple[str, list[str], tuple]] = {}

        # Credits whose decomposition was refused, keyed by value date. One
        # undecomposable batched credit otherwise produces five unrelated-looking
        # register entries -- itself, plus each payout that was probably part of
        # it -- and an operator has to reassemble the situation by hand. Keeping
        # the link lets both sides of the register point at each other.
        self.undecomposable: dict[date, list[tuple[str, int]]] = defaultdict(list)

    # -- helpers ----------------------------------------------------------

    def _window_for(self, settled: date) -> tuple[date, date]:
        """The date range a settlement's bank credit is expected to land in."""
        slack = self.config.window_slack
        earliest = settled - timedelta(days=slack + 2)
        latest = add_banking_days(settled, slack + 1)
        return earliest, latest

    def _wide_window_for(self, settled: date) -> tuple[date, date]:
        """A generous range used only when a reference already corroborates."""
        return (
            settled - timedelta(days=4),
            add_banking_days(settled, self.config.identifier_max_banking_days),
        )

    def _claim(self, *, settlement_ids: Sequence[str], line_ids: Sequence[str]) -> None:
        self.open_settlements.difference_update(settlement_ids)
        self.open_credits.difference_update(line_ids)
        self.open_debits.difference_update(line_ids)

    def _payments_for(self, settlement: Settlement) -> list[str]:
        return [pid for pid in settlement.payment_ids if pid in self.payments]

    def _ledger_for(self, settlement: Settlement) -> list[str]:
        entries = []
        for payment_id in settlement.payment_ids:
            payment = self.payments.get(payment_id)
            if payment and (entry := self.ledger_by_order.get(payment.order_id)):
                entries.append(entry.entry_id)
        return entries

    def _record_match(self, match: Match) -> None:
        self.result.matches.append(match)
        self._claim(
            settlement_ids=match.settlement_ids, line_ids=match.bank_line_ids
        )

    def _record_exception(self, exception: Exception_) -> None:
        self.result.exceptions.append(exception)

    # -- passes -----------------------------------------------------------

    def _pass_quarantine(self) -> None:
        """Surface rows the loader refused, so nothing is silently dropped."""
        for item in self.batch.quarantined:
            self._record_exception(
                Exception_.create(
                    subject_source=Source.BANK,
                    subject_ids=(
                        [item.record_id] if item.record_id
                        else [f"{item.source_file}:{item.row_number}"]
                    ),
                    reason=ReasonCode.MALFORMED_RECORD,
                    amount=0,
                    as_of=date.today(),
                    evidence=[Evidence("parse_error", item.reason, -1.0)],
                    summary_override=(
                        f"Row {item.row_number} of {item.source_file}"
                        + (f" (record {item.record_id})" if item.record_id else "")
                        + f" could not be parsed and was excluded from all "
                          f"totals: {item.reason}"
                    ),
                )
            )

    def _pass_reversals(self) -> None:
        """Net out credit/debit pairs that cancel each other.

        A failed transfer that is returned the same day produces two statement
        lines. Treated independently they become two separate breaks -- an
        unexplained credit and an unexplained debit -- which is twice the noise
        for zero information. Pairing them requires matching amount and a
        shared reference token, so unrelated same-value transactions are not
        collapsed by coincidence.
        """
        by_amount: dict[int, list[str]] = defaultdict(list)
        for line_id in self.open_debits:
            by_amount[abs(self.bank_index.facts[line_id].line.amount)].append(line_id)

        for credit_id in sorted(self.open_credits):
            credit = self.bank_index.facts[credit_id]
            for debit_id in by_amount.get(credit.line.amount, []):
                if debit_id not in self.open_debits:
                    continue
                debit = self.bank_index.facts[debit_id]
                if abs((debit.line.value_date - credit.line.value_date).days) > 3:
                    continue
                shared = credit.tokens & debit.tokens
                if not shared:
                    continue

                self._record_match(
                    Match.create(
                        bank_line_ids=[credit_id, debit_id],
                        settlement_ids=[],
                        tier=Tier.T0_IDENTIFIER,
                        reason=ReasonCode.REVERSAL_NETTED,
                        confidence=0.99,
                        bank_total=credit.line.amount + debit.line.amount,
                        expected_total=0,
                        evidence=[
                            Evidence(
                                "reversal_pair",
                                f"credit of {format_inr(credit.line.amount)} returned the "
                                f"same value date, sharing reference {sorted(shared)[0]}",
                                1.0,
                                (credit_id, debit_id),
                            )
                        ],
                        rationale=(
                            "A credit and an offsetting debit of identical value, "
                            "sharing a reference token, net to zero. Recorded as "
                            "explained rather than raised as two separate breaks."
                        ),
                    )
                )
                self.result.counters["reversals_netted"] += 1
                break

    def _pass_component_audit(self) -> None:
        """Check each payout equals the sum of the parts the PSP says it contains.

        This is the check that catches money held back. A payout can match its
        bank credit perfectly and still be wrong, because the PSP retained part
        of it as a reserve. Matching alone would never notice: both sides agree
        on the reduced figure. Only comparing against the component payments
        reveals the gap.
        """
        for settlement_id, settlement in self.settlement_index.by_id.items():
            known = self._payments_for(settlement)
            if not known or len(known) != len(settlement.payment_ids):
                continue  # cannot verify against components we do not hold

            expected = sum(self.payments[pid].net for pid in known)
            for refund_id in settlement.refund_ids:
                if refund := self.refunds_by_id.get(refund_id):
                    expected -= refund.amount
            for adjustment_id in settlement.adjustment_ids:
                if adjustment := self.adjustments_by_id.get(adjustment_id):
                    expected += adjustment.amount  # already signed

            delta = settlement.amount - expected
            if abs(delta) <= self.config.amount_tolerance:
                continue

            reason = (
                ReasonCode.PARTIAL_SETTLEMENT_OPEN if delta < 0
                else ReasonCode.AMOUNT_MISMATCH
            )
            self._record_exception(
                Exception_.create(
                    subject_source=Source.PSP,
                    subject_ids=[settlement_id],
                    reason=reason,
                    amount=abs(delta),
                    as_of=settlement.settled_on,
                    delta=delta,
                    evidence=[
                        Evidence(
                            "component_sum",
                            f"components total {format_inr(expected)} but the payout "
                            f"is {format_inr(settlement.amount)}",
                            -1.0,
                            tuple(known),
                        )
                    ],
                    summary_override=(
                        f"Payout is {format_inr(abs(delta))} "
                        f"{'short of' if delta < 0 else 'above'} the sum of its "
                        f"{len(known)} component payments net of documented "
                        f"refunds and adjustments."
                    ),
                )
            )
            self.result.counters["component_deviations"] += 1

    def _pass_fee_audit(self) -> None:
        """Recompute every fee against the contracted rate card.

        A reconciler that only matches totals never finds an overcharge,
        because an overcharged payment still settles and still reconciles. It
        is real recoverable money, and it is invisible without independently
        recomputing what the fee should have been.
        """
        overcharged_total = 0
        for payment in self.batch.payments:
            ok, delta, explanation = verify_fee(
                payment.gross, payment.fee, payment.tax, payment.method,
                tolerance=self.config.fee_tolerance,
            )
            if ok:
                continue
            if delta > 0:
                overcharged_total += delta
            self._record_exception(
                Exception_.create(
                    subject_source=Source.PSP,
                    subject_ids=[payment.payment_id],
                    reason=ReasonCode.FEE_SCHEDULE_DEVIATION,
                    amount=abs(delta),
                    as_of=payment.captured_on,
                    delta=delta,
                    evidence=[Evidence("rate_card", explanation, -1.0, (payment.payment_id,))],
                    summary_override=(
                        f"Fee on {payment.method} payment of "
                        f"{format_inr(payment.gross)} deviates from the rate card by "
                        f"{format_inr(abs(delta))}."
                    ),
                )
            )
        self.result.counters["fee_overcharge_paise"] = overcharged_total

    def _pass_fx_audit(self) -> None:
        """Flag cross-currency settlements whose applied rate cannot be checked."""
        for payment in self.batch.payments:
            if not payment.international:
                continue
            self._record_exception(
                Exception_.create(
                    subject_source=Source.PSP,
                    subject_ids=[payment.payment_id],
                    reason=ReasonCode.FX_UNVERIFIED,
                    amount=payment.gross,
                    as_of=payment.captured_on,
                    evidence=[
                        Evidence(
                            "fx_rate",
                            f"settled in INR from {payment.source_currency or 'foreign'} "
                            f"at a stated rate of {payment.fx_rate or 'unknown'}, which "
                            f"no independent source in this batch corroborates",
                            0.0,
                            (payment.payment_id,),
                        )
                    ],
                    summary_override=(
                        f"International payment of {format_inr(payment.gross)} settled "
                        f"at a rate that cannot be verified from the supplied data."
                    ),
                )
            )

    def _pass_ledger_coverage(self) -> None:
        """Find money collected that the internal books never recorded."""
        for payment in self.batch.payments:
            if payment.order_id in self.ledger_by_order:
                continue
            self._record_exception(
                Exception_.create(
                    subject_source=Source.LEDGER,
                    subject_ids=[payment.payment_id],
                    reason=ReasonCode.LEDGER_ENTRY_MISSING,
                    amount=payment.gross,
                    as_of=payment.captured_on,
                    evidence=[
                        Evidence(
                            "ledger_lookup",
                            f"no ledger entry for order {payment.order_id}",
                            -1.0,
                            (payment.payment_id,),
                        )
                    ],
                    summary_override=(
                        f"{format_inr(payment.gross)} collected on order "
                        f"{payment.order_id} has no entry in the internal ledger."
                    ),
                )
            )

    def _pass_unlinked_deductions(self) -> None:
        """Refunds and chargebacks whose original payment is not in this batch."""
        for refund in self.batch.refunds:
            if refund.payment_id in self.payments:
                continue
            self._record_exception(
                Exception_.create(
                    subject_source=Source.PSP,
                    subject_ids=[refund.refund_id],
                    reason=ReasonCode.UNLINKED_REFUND,
                    amount=refund.amount,
                    as_of=refund.created_at.date(),
                    evidence=[
                        Evidence(
                            "payment_lookup",
                            f"refund references payment {refund.payment_id}, absent "
                            f"from this period",
                            -1.0, (refund.refund_id,),
                        )
                    ],
                )
            )

        for adjustment in self.batch.adjustments:
            linked = adjustment.linked_payment_id
            if linked and linked in self.payments:
                continue
            reason = (
                ReasonCode.UNLINKED_CHARGEBACK
                if adjustment.kind in {"chargeback", "dispute_fee"}
                else ReasonCode.UNLINKED_REFUND
            )
            self._record_exception(
                Exception_.create(
                    subject_source=Source.PSP,
                    subject_ids=[adjustment.adjustment_id],
                    reason=reason,
                    amount=abs(adjustment.amount),
                    as_of=adjustment.created_at.date(),
                    evidence=[
                        Evidence(
                            "payment_lookup",
                            f"{adjustment.kind} references payment {linked or 'none'}, "
                            f"absent from this period",
                            -1.0, (adjustment.adjustment_id,),
                        )
                    ],
                )
            )

    def _pass_identifier(self) -> None:
        """Match on a payment reference recovered from the narration.

        The strongest evidence available, so it runs first and is allowed the
        widest date window -- a correct UTR identifies the money regardless of
        when it landed. What it is *not* allowed to do is paper over a
        discrepancy: when the reference agrees and the amount does not, this
        pass classifies the difference and raises it, rather than matching
        anyway on the strength of the reference.
        """
        for settlement_id in sorted(self.open_settlements):
            settlement = self.settlement_index.by_id[settlement_id]
            if not settlement.utr:
                continue

            start, end = self._wide_window_for(settlement.settled_on)
            window = self.bank_index.in_window(start, end) & self.open_credits
            candidate_ids = self.bank_index.reference_candidates(
                settlement.utr, window
            ) & self.open_credits

            scored: list[tuple[float, str, str, str]] = []
            for line_id in candidate_ids:
                line = self.bank_index.facts[line_id].line
                score, mechanism, token = score_reference_match(
                    settlement.utr, line.narration
                )
                if score >= self.config.narration_threshold:
                    scored.append((score, line_id, mechanism, token))
            if not scored:
                continue

            scored.sort(key=lambda entry: (-entry[0], entry[1]))
            best_score = scored[0][0]
            exact_mechanism = scored[0][2] in {"exact_substring", "exact_token"}
            matched_ids = [line_id for _, line_id, _, _ in scored]
            total = sum(
                self.bank_index.facts[line_id].line.amount for line_id in matched_ids
            )

            if len(matched_ids) > 1:
                self._resolve_multi_line(settlement, scored, total)
                continue

            line_id = matched_ids[0]
            line = self.bank_index.facts[line_id].line
            delta = line.amount - settlement.amount
            evidence = [
                Evidence(
                    "reference_match",
                    f"settlement UTR {settlement.utr} recovered from narration by "
                    f"{scored[0][2]} (score {best_score:.2f}) as {scored[0][3]}",
                    best_score,
                    (settlement_id, line_id),
                )
            ]

            lateness = banking_days_between(settlement.settled_on, line.value_date)
            if lateness > self.config.identifier_max_banking_days:
                self._record_exception(
                    Exception_.create(
                        subject_source=Source.PSP,
                        subject_ids=[settlement_id],
                        reason=ReasonCode.DATE_OUT_OF_WINDOW,
                        amount=settlement.amount,
                        as_of=settlement.settled_on,
                        candidates=[line_id],
                        delta=lateness,
                        evidence=evidence + [
                            Evidence(
                                "sla",
                                f"credit landed {lateness} banking days after the payout "
                                f"date, against a T+{self.config.sla_days} contract",
                                -1.0, (line_id,),
                            )
                        ],
                        summary_override=(
                            f"Reference and amount agree, but the credit arrived "
                            f"{lateness} banking days late -- outside the "
                            f"T+{self.config.sla_days} settlement SLA."
                        ),
                    )
                )
                self._claim(settlement_ids=[settlement_id], line_ids=[line_id])
                self.result.counters["sla_breaches"] += 1
                continue

            if delta == 0:
                self._record_match(
                    Match.create(
                        bank_line_ids=[line_id], settlement_ids=[settlement_id],
                        payment_ids=self._payments_for(settlement),
                        ledger_entry_ids=self._ledger_for(settlement),
                        tier=Tier.T0_IDENTIFIER if exact_mechanism else Tier.T1_NARRATION,
                        reason=(
                            ReasonCode.UTR_EXACT if exact_mechanism
                            else ReasonCode.UTR_IN_NARRATION
                        ),
                        confidence=best_score,
                        bank_total=line.amount, expected_total=settlement.amount,
                        evidence=evidence,
                        rationale=(
                            f"The payout reference appears in the bank narration and "
                            f"the amounts agree to the paise."
                        ),
                    )
                )
                continue

            if abs(delta) <= self.config.amount_tolerance:
                self._record_match(
                    Match.create(
                        bank_line_ids=[line_id], settlement_ids=[settlement_id],
                        payment_ids=self._payments_for(settlement),
                        ledger_entry_ids=self._ledger_for(settlement),
                        tier=Tier.T3_TOLERANCE,
                        reason=ReasonCode.ROUNDING_TOLERANCE,
                        confidence=min(best_score, 0.95),
                        bank_total=line.amount, expected_total=settlement.amount,
                        evidence=evidence + [
                            Evidence(
                                "rounding",
                                f"{delta:+d} paise difference, within the "
                                f"{self.config.amount_tolerance} paise tolerance",
                                0.5, (line_id,),
                            )
                        ],
                        rationale=(
                            f"Reference matches and the {abs(delta)} paise difference is "
                            f"consistent with the two sides rounding a fee split "
                            f"differently."
                        ),
                    )
                )
                continue

            self._raise_amount_break(settlement, line_id, line.amount, delta, evidence)

    def _resolve_multi_line(
        self, settlement: Settlement, scored: list[tuple[float, str, str, str]], total: int
    ) -> None:
        """Decide what several lines carrying one reference actually mean.

        Three possibilities, and telling them apart matters a great deal: the
        payout was split across transfers (they sum to it), it was double-posted
        (each equals it), or something else is going on (neither).
        """
        settlement_id = settlement.settlement_id
        line_ids = [line_id for _, line_id, _, _ in scored]
        amounts = [self.bank_index.facts[line_id].line.amount for line_id in line_ids]
        base_evidence = [
            Evidence(
                "reference_match",
                f"reference {settlement.utr} recovered from {len(line_ids)} separate "
                f"bank lines",
                scored[0][0], tuple([settlement_id] + line_ids),
            )
        ]

        if abs(total - settlement.amount) <= self.config.amount_tolerance:
            self._record_match(
                Match.create(
                    bank_line_ids=line_ids, settlement_ids=[settlement_id],
                    payment_ids=self._payments_for(settlement),
                    ledger_entry_ids=self._ledger_for(settlement),
                    tier=Tier.T5_SUBSET_SUM,
                    reason=ReasonCode.SPLIT_SETTLEMENT,
                    confidence=0.93,
                    bank_total=total, expected_total=settlement.amount,
                    evidence=base_evidence + [
                        Evidence(
                            "split_sum",
                            "the parts sum to the payout: "
                            + " + ".join(format_inr(a) for a in amounts)
                            + f" = {format_inr(total)}",
                            1.0, tuple(line_ids),
                        )
                    ],
                    rationale=(
                        f"One payout arrived as {len(line_ids)} transfers carrying the "
                        f"same reference, and they sum to the payout amount."
                    ),
                )
            )
            return

        if all(amount == settlement.amount for amount in amounts):
            self._record_exception(
                Exception_.create(
                    subject_source=Source.BANK,
                    subject_ids=line_ids,
                    reason=ReasonCode.DUPLICATE_IDENTIFIER,
                    amount=settlement.amount,
                    as_of=settlement.settled_on,
                    candidates=[settlement_id],
                    delta=total - settlement.amount,
                    evidence=base_evidence + [
                        Evidence(
                            "duplicate",
                            f"each of the {len(line_ids)} lines equals the full payout "
                            f"of {format_inr(settlement.amount)}; the account was "
                            f"credited {format_inr(total)} in total",
                            -1.0, tuple(line_ids),
                        )
                    ],
                    summary_override=(
                        f"The same reference credited {len(line_ids)} times for the "
                        f"full payout amount. {format_inr(total - settlement.amount)} "
                        f"appears to be a double-post."
                    ),
                )
            )
            self._claim(settlement_ids=[settlement_id], line_ids=line_ids)
            self.result.counters["duplicate_credits"] += 1
            return

        self._record_exception(
            Exception_.create(
                subject_source=Source.PSP,
                subject_ids=[settlement_id],
                reason=ReasonCode.AMBIGUOUS_CANDIDATES,
                amount=settlement.amount,
                as_of=settlement.settled_on,
                candidates=line_ids,
                delta=total - settlement.amount,
                evidence=base_evidence,
                summary_override=(
                    f"{len(line_ids)} bank lines carry this reference but neither sum "
                    f"to nor individually equal the payout."
                ),
            )
        )

    def _raise_amount_break(
        self, settlement: Settlement, line_id: str, bank_amount: int,
        delta: int, evidence: list[Evidence],
    ) -> None:
        """Classify a reference-confirmed amount discrepancy by its signature.

        The difference between "off by 100x", "digits swapped" and "just short"
        is the difference between an engineering bug, a keying error and a
        missing deduction. They go to different people, so naming which one it
        is saves the triage step entirely.
        """
        settlement_id = settlement.settlement_id
        if looks_like_scale_error(bank_amount, settlement.amount):
            reason = ReasonCode.SCALE_ERROR_SUSPECTED
            note = (
                f"bank line reads {format_inr(bank_amount)} against a payout of "
                f"{format_inr(settlement.amount)} -- a factor of exactly 100"
            )
        elif digit_transposition(bank_amount, settlement.amount):
            reason = ReasonCode.TRANSPOSITION_SUSPECTED
            note = (
                f"{format_inr(bank_amount)} and {format_inr(settlement.amount)} use "
                f"the same digits in a different order"
            )
        else:
            reason = ReasonCode.AMOUNT_MISMATCH
            note = (
                f"credit is {format_inr(abs(delta))} "
                f"{'short of' if delta < 0 else 'above'} the payout, with no refund "
                f"or adjustment on file to account for it"
            )

        self._record_exception(
            Exception_.create(
                subject_source=Source.PSP,
                subject_ids=[settlement_id],
                reason=reason,
                amount=settlement.amount,
                as_of=settlement.settled_on,
                candidates=[line_id],
                delta=delta,
                evidence=evidence + [Evidence("amount_delta", note, -1.0, (line_id,))],
                summary_override=note[0].upper() + note[1:],
            )
        )
        self._claim(settlement_ids=[settlement_id], line_ids=[line_id])

    def _pass_unique_amount(self) -> None:
        """Match on amount and date when no reference survived.

        Uniqueness is checked from **both** directions. A settlement having
        exactly one candidate credit is not enough: if that credit also fits a
        second settlement equally well, choosing either is a coin flip dressed
        up as a match. Requiring mutual uniqueness is what turns this pass from
        a plausible guess into evidence.
        """
        for settlement_id in sorted(self.open_settlements):
            settlement = self.settlement_index.by_id[settlement_id]
            start, end = self._window_for(settlement.settled_on)
            line_window = self.bank_index.in_window(start, end) & self.open_credits
            line_candidates = self.bank_index.amount_candidates(
                settlement.amount, line_window
            )
            if not line_candidates:
                continue

            settlement_window = self.settlement_index.in_window(
                start - timedelta(days=2), end + timedelta(days=2)
            ) & self.open_settlements
            rival_settlements = self.settlement_index.amount_candidates(
                settlement.amount, settlement_window
            )

            if len(line_candidates) == 1 and len(rival_settlements) == 1:
                line_id = next(iter(line_candidates))
                line = self.bank_index.facts[line_id].line
                self._record_match(
                    Match.create(
                        bank_line_ids=[line_id], settlement_ids=[settlement_id],
                        payment_ids=self._payments_for(settlement),
                        ledger_entry_ids=self._ledger_for(settlement),
                        tier=Tier.T2_AMOUNT_DATE,
                        reason=ReasonCode.AMOUNT_DATE_UNIQUE,
                        confidence=0.88,
                        bank_total=line.amount, expected_total=settlement.amount,
                        evidence=[
                            Evidence(
                                "unique_amount",
                                f"{format_inr(settlement.amount)} on "
                                f"{line.value_date} is the only amount-and-date match "
                                f"in either direction within the window",
                                0.88, (settlement_id, line_id),
                            )
                        ],
                        rationale=(
                            "No reference survived in the narration, but this amount "
                            "appears exactly once on each side inside the settlement "
                            "window, so the pairing is forced."
                        ),
                    )
                )
                continue

            if len(rival_settlements) > 1 and len(line_candidates) >= 1:
                self._record_exception(
                    Exception_.create(
                        subject_source=Source.PSP,
                        subject_ids=sorted(rival_settlements),
                        reason=ReasonCode.AMBIGUOUS_CANDIDATES,
                        amount=settlement.amount,
                        as_of=settlement.settled_on,
                        candidates=sorted(line_candidates),
                        evidence=[
                            Evidence(
                                "ambiguity",
                                f"{len(rival_settlements)} payouts of exactly "
                                f"{format_inr(settlement.amount)} in the same window "
                                f"and {len(line_candidates)} candidate credit(s); no "
                                f"reference distinguishes them",
                                0.0, tuple(sorted(rival_settlements)),
                            )
                        ],
                        summary_override=(
                            f"{len(rival_settlements)} payouts of identical value fall "
                            f"in the same window with no reference on any of them. "
                            f"Matching would be a coin flip."
                        ),
                    )
                )
                self.open_settlements.difference_update(rival_settlements)
                self.open_credits.difference_update(line_candidates)
                self.result.counters["ambiguous_groups"] += 1

    def _pass_batch_decomposition(self) -> None:
        """Explain a single credit as the sum of several payouts.

        Runs after the one-to-one passes on purpose. A settlement that matches
        a credit outright should be claimed by that simpler explanation first;
        left in the pool, it becomes a component that makes spurious
        decompositions reachable.
        """
        # Credits we genuinely tried to decompose, so that whatever the attempt
        # concluded, the payouts left behind can point back at them.
        attempted: dict[str, int] = {}

        for line_id in sorted(self.open_credits):
            line = self.bank_index.facts[line_id].line
            start = line.value_date - timedelta(days=self.config.window_slack)
            end = line.value_date
            pool_ids = self.settlement_index.in_window(start, end) & self.open_settlements

            # A payout carrying its own reference was transferred on its own:
            # that reference identifies its own movement of money, so it cannot
            # also be a component of some other transfer. Excluding them is a
            # statement about how settlement works, not a heuristic, and it
            # shrinks the candidate pool enough to make larger decompositions
            # trustworthy again -- the pool size is what governs whether a sum
            # is evidence at all.
            #
            # Payouts whose reference went unmatched are still breaks; they are
            # reported by the leftover pass rather than absorbed here.
            unreferenced = {
                sid for sid in pool_ids
                if not self.settlement_index.by_id[sid].utr
            }
            pool_ids = unreferenced if len(unreferenced) >= 2 else pool_ids
            if len(pool_ids) < 2:
                continue
            attempted[line_id] = line.amount

            items = [
                Item(sid, self.settlement_index.by_id[sid].amount) for sid in sorted(pool_ids)
            ]
            # How many components may be combined depends on how many payouts
            # are competing. A large pool makes big combinations meaningless,
            # so the cardinality shrinks rather than the answer becoming a
            # guess dressed up as a decomposition.
            cardinality = credible_cardinality(
                len(items), self.config.max_batch_cardinality
            )
            if cardinality < 2:
                continue

            outcome = find_subsets(
                items, line.amount,
                tolerance=self.config.batch_tolerance,
                max_cardinality=cardinality,
            )

            # An exact decomposition beats a near one outright. When several
            # combinations land inside the tolerance but only one balances to
            # the paise, that one is not merely the best of a field -- the
            # others are arithmetically worse explanations of the same credit,
            # and treating the situation as ambiguous discards a real answer.
            # Two or more *exact* combinations is different: that is genuine
            # ambiguity, and it still refuses.
            exact = [s for s in outcome.solutions if s.is_exact]
            unique_exact = len(exact) == 1 and not outcome.truncated

            # A solution found in a pool large enough to hit any target by
            # chance is not evidence, however unique it looks inside the
            # enumeration. Refusing here is what keeps decomposition honest as
            # volume grows -- it is the pass most able to invent a plausible
            # answer, so it is the one that has to prove it did not.
            if outcome.solutions and not outcome.is_credible:
                self.result.counters["decompositions_refused_as_chance"] += 1
                self._record_exception(
                    Exception_.create(
                        subject_source=Source.BANK,
                        subject_ids=[line_id],
                        reason=ReasonCode.AMBIGUOUS_CANDIDATES,
                        amount=line.amount,
                        as_of=line.value_date,
                        candidates=sorted(outcome.solutions[0].refs),
                        evidence=[
                            Evidence(
                                "chance_decomposition",
                                f"a combination summing to this credit exists, but "
                                f"among {outcome.pool_size} candidate payouts roughly "
                                f"{outcome.expected_spurious:.1f} combinations would "
                                f"reach any given total by chance, so the match proves "
                                f"nothing",
                                0.0, tuple(outcome.solutions[0].refs),
                            )
                        ],
                        summary_override=(
                            f"This credit is decomposable, but the candidate pool of "
                            f"{outcome.pool_size} payouts is large enough that a "
                            f"combination would land on it by coincidence. Narrow the "
                            f"period or supply the payout reference to resolve it."
                        ),
                    )
                )
                self.open_credits.discard(line_id)
                continue

            if outcome.is_unique or unique_exact:
                solution = exact[0] if unique_exact else outcome.solutions[0]
                if solution.cardinality < 2:
                    continue  # a single component is the previous pass's job
                members = list(solution.refs)
                payment_ids: list[str] = []
                ledger_ids: list[str] = []
                for sid in members:
                    settlement = self.settlement_index.by_id[sid]
                    payment_ids.extend(self._payments_for(settlement))
                    ledger_ids.extend(self._ledger_for(settlement))

                self._record_match(
                    Match.create(
                        bank_line_ids=[line_id], settlement_ids=members,
                        payment_ids=payment_ids, ledger_entry_ids=ledger_ids,
                        tier=Tier.T5_SUBSET_SUM,
                        reason=ReasonCode.BATCH_SUBSET_SUM,
                        confidence=0.90,
                        bank_total=line.amount, expected_total=solution.total,
                        evidence=[
                            Evidence(
                                "decomposition",
                                f"{len(members)} of the {outcome.pool_size} payouts in "
                                f"this window total {format_inr(solution.total)}, "
                                + (
                                    "the only combination balancing to the paise"
                                    if unique_exact and len(outcome.solutions) > 1
                                    else "the only combination that reaches the credit"
                                ),
                                0.90, tuple(members),
                            )
                        ],
                        rationale=(
                            f"The credit of {format_inr(line.amount)} is a batched "
                            f"payout. Of every combination of up to "
                            f"{self.config.max_batch_cardinality} payouts in the "
                            f"window, exactly one reaches this total, so the "
                            f"decomposition is forced rather than chosen."
                        ),
                    )
                )
                self.result.counters["batches_decomposed"] += 1

            elif len(outcome.solutions) > 1:
                self._record_exception(
                    Exception_.create(
                        subject_source=Source.BANK,
                        subject_ids=[line_id],
                        reason=ReasonCode.AMBIGUOUS_CANDIDATES,
                        amount=line.amount,
                        as_of=line.value_date,
                        candidates=sorted({r for s in outcome.solutions for r in s.refs}),
                        evidence=[
                            Evidence(
                                "ambiguous_decomposition",
                                f"{len(outcome.solutions)} distinct combinations of "
                                f"payouts reach {format_inr(line.amount)}; the data "
                                f"does not say which one occurred",
                                0.0, tuple(outcome.solutions[0].refs),
                            )
                        ],
                        summary_override=(
                            f"This credit can be decomposed "
                            f"{len(outcome.solutions)} different ways. Reporting any "
                            f"one of them as fact would be a guess."
                        ),
                    )
                )
                self.open_credits.discard(line_id)
                self.result.counters["ambiguous_decompositions"] += 1

        # Whatever the reason -- the pool was too large to trust a combination,
        # no combination reached the total, or several did -- a credit still
        # open after this pass is one we tried and could not break down. The
        # payouts that were candidates for it are almost certainly its
        # components, and saying so is far more useful to an operator than
        # reporting each of them as an unrelated missing payout.
        for line_id, amount in attempted.items():
            if line_id in self.open_credits:
                value_date = self.bank_index.facts[line_id].line.value_date
                self.undecomposable[value_date].append((line_id, amount))
                self.result.counters["credits_not_decomposable"] += 1

    def _pass_tolerance_amount(self) -> None:
        """Last deterministic pass: near-amount, unique, inside the window."""
        for settlement_id in sorted(self.open_settlements):
            settlement = self.settlement_index.by_id[settlement_id]
            start, end = self._window_for(settlement.settled_on)
            window = self.bank_index.in_window(start, end) & self.open_credits
            candidates = self.bank_index.near_amount_candidates(
                settlement.amount, window, self.config.amount_tolerance
            )
            if len(candidates) != 1:
                continue
            line_id = next(iter(candidates))
            line = self.bank_index.facts[line_id].line
            delta = line.amount - settlement.amount
            self._record_match(
                Match.create(
                    bank_line_ids=[line_id], settlement_ids=[settlement_id],
                    payment_ids=self._payments_for(settlement),
                    ledger_entry_ids=self._ledger_for(settlement),
                    tier=Tier.T3_TOLERANCE,
                    reason=ReasonCode.ROUNDING_TOLERANCE,
                    confidence=0.80,
                    bank_total=line.amount, expected_total=settlement.amount,
                    evidence=[
                        Evidence(
                            "near_amount",
                            f"sole credit within {self.config.amount_tolerance} paise "
                            f"of the payout in the window ({delta:+d} paise)",
                            0.80, (settlement_id, line_id),
                        )
                    ],
                )
            )

    def _pass_adjudication(self) -> None:
        """Hand the residual to the adjudicator, one record at a time.

        By this point the deterministic cascade has taken everything it can
        justify. What remains is genuinely hard, and it is small -- which is
        the whole point of the ordering, because it is the only part that costs
        anything per record.
        """
        if not (self.adjudicator and self.config.enable_adjudicator):
            return
        self.result.adjudicator_name = self.adjudicator.name

        for settlement_id in sorted(self.open_settlements):
            settlement = self.settlement_index.by_id[settlement_id]
            start, end = self._window_for(settlement.settled_on)
            window = self.bank_index.in_window(start, end) & self.open_credits
            if not window:
                continue

            # Rank cheaply first and only build the expensive evidence for a
            # shortlist. Handing an adjudicator ninety candidates is both slow
            # and pointless: nothing decides confidently among ninety, and the
            # rate-card inversion needed to describe each one dominated the
            # whole run at volume.
            ranked: list[tuple[float, int, str]] = []
            for line_id in window:
                line = self.bank_index.facts[line_id].line
                score, _, _ = score_reference_match(
                    settlement.utr or "", line.narration
                )
                ranked.append((-score, abs(line.amount - settlement.amount), line_id))
            ranked.sort()
            shortlist = [line_id for _, _, line_id in ranked[:self.config.adjudicator_candidates]]

            candidates = []
            for line_id in shortlist:
                line = self.bank_index.facts[line_id].line
                score, mechanism, token = score_reference_match(
                    settlement.utr or "", line.narration
                )
                delta = line.amount - settlement.amount
                inversions = (
                    invert_across_methods(line.amount)
                    if abs(delta) > self.config.amount_tolerance else {}
                )
                candidates.append({
                    "id": line_id,
                    "amount": line.amount,
                    "date": line.value_date.isoformat(),
                    "narration": line.narration,
                    "delta": delta,
                    "reference_score": score,
                    "reference_mechanism": mechanism,
                    "matched_token": token,
                    "banking_days_late": banking_days_between(
                        settlement.settled_on, line.value_date
                    ),
                    "scale_error": looks_like_scale_error(line.amount, settlement.amount),
                    "transposition": digit_transposition(line.amount, settlement.amount),
                    "fee_inversions": {m: g[:2] for m, g in list(inversions.items())[:3]},
                })

            request = AdjudicationRequest(
                subject_kind="settlement",
                subject_id=settlement_id,
                subject_amount=settlement.amount,
                subject_date=settlement.settled_on,
                subject_description=(
                    f"Payout {settlement_id} of {format_inr(settlement.amount)} dated "
                    f"{settlement.settled_on}, reference {settlement.utr or 'absent'}"
                ),
                candidates=tuple(candidates),
            )
            outcome = self.adjudicator.adjudicate(request)
            self.result.counters["adjudicated"] += 1

            if outcome.decision != "match" or not outcome.chosen_ids:
                # An abstention is not itself a break. The record simply stays
                # open and the leftover pass names the underlying problem --
                # raising a separate exception here would put two entries on
                # the register for one unreconciled payout, which is noise an
                # operator has to clear before doing any actual work. The
                # reasoning is kept and attached to the real exception instead.
                self.abstentions[settlement_id] = (
                    outcome.rationale, [c["id"] for c in candidates], outcome.evidence
                )
                self.result.counters["adjudicator_abstained"] += 1
                continue

            chosen = list(outcome.chosen_ids)
            total = sum(self.bank_index.facts[lid].line.amount for lid in chosen)
            self._record_match(
                Match.create(
                    bank_line_ids=chosen, settlement_ids=[settlement_id],
                    payment_ids=self._payments_for(settlement),
                    ledger_entry_ids=self._ledger_for(settlement),
                    tier=Tier.T7_ADJUDICATED,
                    reason=ReasonCode.ADJUDICATED_MATCH,
                    confidence=outcome.confidence,
                    bank_total=total, expected_total=settlement.amount,
                    evidence=outcome.evidence,
                    rationale=outcome.rationale,
                    adjudicator=self.adjudicator.name,
                )
            )
            self.result.counters["adjudicated_matches"] += 1

    def _pass_leftovers(self) -> None:
        """Everything still open is a break. Name it precisely."""
        for settlement_id in sorted(self.open_settlements):
            settlement = self.settlement_index.by_id[settlement_id]
            start, end = self._window_for(settlement.settled_on)
            window = self.bank_index.in_window(start, end) & self.open_credits

            closest = None
            if window:
                items = [
                    Item(lid, self.bank_index.facts[lid].line.amount)
                    for lid in sorted(window)
                ]
                closest = closest_subset(items, settlement.amount, max_cardinality=3)

            evidence = [
                Evidence(
                    "search",
                    f"{len(window)} credit(s) in the T+{self.config.sla_days} window; "
                    f"none matched by reference, amount, or decomposition",
                    -1.0, (settlement_id,),
                )
            ]
            if closest is not None and abs(closest.residual) < settlement.amount:
                evidence.append(
                    Evidence(
                        "nearest",
                        f"closest reachable combination is "
                        f"{format_inr(abs(closest.residual))} "
                        f"{'over' if closest.residual > 0 else 'short'} using "
                        f"{closest.cardinality} credit(s)",
                        0.0, closest.refs,
                    )
                )

            # If a credit on this date could not be decomposed, this payout is
            # very likely one of its components. Saying so turns an isolated
            # "payout never arrived" into a lead the operator can act on.
            nearby = self.undecomposable.get(settlement.settled_on, [])
            if nearby:
                credits = ", ".join(cid for cid, _ in nearby[:3])
                evidence.append(
                    Evidence(
                        "undecomposable_credit",
                        f"{len(nearby)} credit(s) on this date could not be broken "
                        f"into components without a payout reference ({credits}); "
                        f"this payout may be part of one of them",
                        0.0, tuple(cid for cid, _ in nearby[:3]),
                    )
                )

            reviewed: list[str] = []
            if abstention := self.abstentions.get(settlement_id):
                rationale, reviewed, signals = abstention
                evidence.append(
                    Evidence(
                        "adjudicator",
                        f"reviewed by {self.result.adjudicator_name} and left open: "
                        f"{rationale}",
                        0.0, (settlement_id,),
                    )
                )
                evidence.extend(signals)

            # The break is that this payout has no bank credit. Whether other,
            # unrelated credits happen to sit in the same window changes
            # nothing about that, and routing on it would send identical
            # situations to two different reason codes -- and so to two
            # different owners -- for no reason an operator could act on.
            self._record_exception(
                Exception_.create(
                    subject_source=Source.PSP,
                    subject_ids=[settlement_id],
                    reason=ReasonCode.MISSING_BANK_CREDIT,
                    amount=settlement.amount,
                    as_of=settlement.settled_on,
                    candidates=sorted(set(window) | set(reviewed)),
                    evidence=evidence,
                )
            )

        for line_id in sorted(self.open_credits):
            line = self.bank_index.facts[line_id].line
            self._record_exception(
                Exception_.create(
                    subject_source=Source.BANK,
                    subject_ids=[line_id],
                    reason=ReasonCode.UNEXPECTED_BANK_CREDIT,
                    amount=line.amount,
                    as_of=line.value_date,
                    evidence=[
                        Evidence(
                            "narration",
                            f"no payout explains this credit; narration reads "
                            f"{line.narration[:90]!r}",
                            -1.0, (line_id,),
                        )
                    ],
                    summary_override=(
                        f"{format_inr(line.amount)} credited on {line.value_date} that "
                        f"no settlement in this period accounts for."
                    ),
                )
            )

    # -- driver -----------------------------------------------------------

    def run(self) -> ReconResult:
        """Execute the cascade and return everything it concluded."""
        started = time.perf_counter()
        self.result.records_considered = self.batch.record_count

        stages: tuple[tuple[str, Callable[[], None]], ...] = (
            ("quarantine", self._pass_quarantine),
            ("reversals", self._pass_reversals),
            ("component_audit", self._pass_component_audit),
            ("fee_audit", self._pass_fee_audit),
            ("fx_audit", self._pass_fx_audit),
            ("ledger_coverage", self._pass_ledger_coverage),
            ("unlinked_deductions", self._pass_unlinked_deductions),
            ("identifier", self._pass_identifier),
            ("unique_amount", self._pass_unique_amount),
            ("batch_decomposition", self._pass_batch_decomposition),
            ("tolerance_amount", self._pass_tolerance_amount),
            ("adjudication", self._pass_adjudication),
            ("leftovers", self._pass_leftovers),
        )
        for name, stage in stages:
            stage_start = time.perf_counter()
            stage()
            self.result.stage_timings[name] = time.perf_counter() - stage_start

        self.result.total_seconds = time.perf_counter() - started
        return self.result
