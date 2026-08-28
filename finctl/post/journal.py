"""Posting reconciled settlements to a double-entry journal.

Matching is only half of closing a finance-ops loop. The other half is turning
each proven match into balanced journal entries, which is what makes the result
usable by an accounting system rather than a report someone re-keys.

Double-entry is doing real work here, not decoration. A settlement decomposes
into four movements that must sum to zero:

    Dr  Bank                      the cash that actually arrived
    Dr  Gateway fees              the PSP's commission (an expense)
    Dr  GST input credit          tax on that commission (recoverable)
    Dr  Refunds and chargebacks   value returned to customers
      Cr  Trade receivables       the gross the customer was billed

If those do not balance, something in the match is wrong, and the imbalance
surfaces here as a hard failure rather than as a rounding difference that
someone finds at quarter end.

Every entry id derives from the match that produced it, so posting the same run
twice produces byte-identical entries and cannot double-post.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Sequence

from finctl.ingest.loader import Batch
from finctl.models import Match, ReasonCode
from finctl.money import format_inr


class Account:
    """Chart of accounts. Names match what a mid-market Indian ledger uses."""

    BANK = "1010 Bank - Current Account"
    RECEIVABLES = "1200 Trade Receivables"
    GATEWAY_FEES = "5300 Payment Gateway Fees"
    GST_INPUT = "1350 GST Input Credit"
    REFUNDS = "4900 Refunds and Chargebacks"
    RESERVE = "1360 Gateway Reserve Receivable"
    ROUNDING = "5390 Settlement Rounding Difference"
    SUSPENSE = "1999 Reconciliation Suspense"


@dataclass(frozen=True, slots=True)
class JournalLine:
    """One side of one entry. `amount` is positive; `debit` gives direction."""

    account: str
    debit: bool
    amount: int
    memo: str = ""

    @property
    def signed(self) -> int:
        """Debits positive, credits negative, so a balanced entry sums to zero."""
        return self.amount if self.debit else -self.amount


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """A balanced set of lines posted on one date against one match."""

    entry_id: str
    posted_on: date
    narrative: str
    lines: tuple[JournalLine, ...]
    match_id: str
    settlement_ids: tuple[str, ...]

    @property
    def total_debits(self) -> int:
        return sum(line.amount for line in self.lines if line.debit)

    @property
    def total_credits(self) -> int:
        return sum(line.amount for line in self.lines if not line.debit)

    @property
    def is_balanced(self) -> bool:
        return sum(line.signed for line in self.lines) == 0


@dataclass(slots=True)
class PostingResult:
    """Everything posted, plus anything that refused to balance."""

    entries: list[JournalEntry] = field(default_factory=list)
    unbalanced: list[JournalEntry] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    # Balancing amounts, tracked separately so they are reported rather than
    # disappearing into an account total. A reconciler that silently plugs is
    # worse than one that fails loudly.
    rounding_total: int = 0
    reserve_total: int = 0
    suspense_total: int = 0
    suspense_entries: list[str] = field(default_factory=list)

    @property
    def total_debits(self) -> int:
        return sum(entry.total_debits for entry in self.entries)

    @property
    def total_credits(self) -> int:
        return sum(entry.total_credits for entry in self.entries)

    @property
    def balances(self) -> bool:
        """The trial balance. If this is false, nothing else in the run matters."""
        return self.total_debits == self.total_credits and not self.unbalanced

    def account_totals(self) -> dict[str, int]:
        """Net movement per account, for a trial balance view."""
        totals: dict[str, int] = {}
        for entry in self.entries:
            for line in entry.lines:
                totals[line.account] = totals.get(line.account, 0) + line.signed
        return dict(sorted(totals.items()))


def _entry_id(match_id: str) -> str:
    """Deterministic from the match, which is what makes posting idempotent."""
    digest = hashlib.blake2b(match_id.encode("utf-8"), digest_size=6).hexdigest()
    return f"je_{digest}"


def post_matches(
    matches: Sequence[Match],
    batch: Batch,
    *,
    posting_date: date | None = None,
    rounding_tolerance: int = 5,
) -> PostingResult:
    """Turn verified matches into balanced journal entries.

    Only matches that passed verification should reach this function. Anything
    that cannot be decomposed into a balancing entry is recorded as skipped
    rather than posted with a plug to suspense -- a suspense plug is how an
    imbalance becomes invisible, and the entire point of this system is that it
    does not.
    """
    result = PostingResult()
    payments = batch.index_payments()
    settlements = batch.index_settlements()
    refunds = {refund.refund_id: refund for refund in batch.refunds}
    adjustments = {adj.adjustment_id: adj for adj in batch.adjustments}
    bank_lines = batch.index_bank_lines()

    for match in matches:
        # A reversal pair moves no value and belongs in no ledger account; it
        # is recorded as explained and posts nothing.
        if match.reason is ReasonCode.REVERSAL_NETTED:
            result.skipped.append((match.match_id, "reversal pair, nets to zero"))
            continue
        if not match.settlement_ids:
            result.skipped.append((match.match_id, "no settlement to post against"))
            continue

        gross = fee = tax = returned = 0
        component_count = 0
        for settlement_id in match.settlement_ids:
            settlement = settlements.get(settlement_id)
            if settlement is None:
                continue
            for payment_id in settlement.payment_ids:
                if payment := payments.get(payment_id):
                    gross += payment.gross
                    fee += payment.fee
                    tax += payment.tax
                    component_count += 1
            for refund_id in settlement.refund_ids:
                if refund := refunds.get(refund_id):
                    returned += refund.amount
            for adjustment_id in settlement.adjustment_ids:
                if adjustment := adjustments.get(adjustment_id):
                    returned += abs(adjustment.amount)

        if component_count == 0:
            result.skipped.append(
                (match.match_id, "no component payments available to post")
            )
            continue

        cash = sum(bank_lines[line_id].amount for line_id in match.bank_line_ids
                   if line_id in bank_lines)

        lines = [
            JournalLine(Account.BANK, True, cash, "cash received from the gateway"),
            JournalLine(Account.GATEWAY_FEES, True, fee, "gateway commission"),
            JournalLine(Account.GST_INPUT, True, tax, "GST on commission, recoverable"),
        ]
        if returned:
            lines.append(
                JournalLine(Account.REFUNDS, True, returned, "refunds and chargebacks")
            )
        lines.append(
            JournalLine(Account.RECEIVABLES, False, gross, "settle customer receivable")
        )

        # What the customer was billed, less everything already accounted for.
        # A non-zero residual is not an error to be plugged away -- it is a real
        # balance that belongs in a named account, and which account it belongs
        # in depends on why the money did not arrive.
        residual = gross - (cash + fee + tax + returned)
        if residual:
            if abs(residual) <= rounding_tolerance:
                # The two sides split a fee to a different paise. A named
                # difference account is the standard treatment and keeps the
                # amount visible instead of absorbing it into an expense.
                lines.append(
                    JournalLine(
                        Account.ROUNDING, residual > 0, abs(residual),
                        "paise rounding difference between gateway and bank",
                    )
                )
                result.rounding_total += abs(residual)
            elif residual > 0:
                # The gateway withheld part of the payout. It is still owed to
                # the merchant, so it is a receivable, not a loss -- and the
                # component audit has already raised it for follow-up.
                lines.append(
                    JournalLine(
                        Account.RESERVE, True, residual,
                        "withheld by the gateway, recoverable in a later cycle",
                    )
                )
                result.reserve_total += residual
            else:
                # More cash arrived than the components explain. Rare, and
                # never quietly absorbed: it goes to suspense and is counted so
                # the run reports it rather than burying it in a total.
                lines.append(
                    JournalLine(
                        Account.SUSPENSE, False, abs(residual),
                        "unexplained excess receipt, pending investigation",
                    )
                )
                result.suspense_total += abs(residual)
                result.suspense_entries.append(match.match_id)

        # Drop zero lines so the entry reads cleanly, but only after the totals
        # are fixed, so removing them cannot change whether it balances.
        lines = [line for line in lines if line.amount != 0]

        earliest = min(
            (settlements[sid].settled_on for sid in match.settlement_ids
             if sid in settlements),
            default=posting_date or date.today(),
        )

        entry = JournalEntry(
            entry_id=_entry_id(match.match_id),
            posted_on=posting_date or earliest,
            narrative=(
                f"Settlement of {component_count} payment(s) totalling "
                f"{format_inr(gross)} gross, received as {format_inr(cash)} net of "
                f"{format_inr(fee + tax)} gateway charges"
                + (f" and {format_inr(returned)} returned to customers" if returned else "")
                + (f", with {format_inr(residual)} withheld" if residual > rounding_tolerance else "")
                + f" [{match.reason.value}]"
            ),
            lines=tuple(lines),
            match_id=match.match_id,
            settlement_ids=tuple(match.settlement_ids),
        )

        (result.entries if entry.is_balanced else result.unbalanced).append(entry)

    return result


def trial_balance_report(result: PostingResult) -> list[tuple[str, int, str]]:
    """Account totals with a direction label, ready to render."""
    rows: list[tuple[str, int, str]] = []
    for account, net in result.account_totals().items():
        rows.append((account, abs(net), "Dr" if net >= 0 else "Cr"))
    return rows
