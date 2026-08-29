"""Synthetic settlement data with a held-out ground truth.

Builds a merchant's month: customer payments, PSP payout batches, the bank
statement those batches land on, and the internal ledger that was supposed to
record all of it -- then perturbs the result according to the scenario
catalogue so that every awkward case a real reconciler meets is present in
known quantity.

Two properties make the output worth measuring against.

**The messiness is in the format, not just the content.** Each simulated bank
emits its own date format, its own amount formatting, and its own narration
template. An engine that only works because the generator and the parser share
assumptions has proven nothing, so the generator deliberately does not share
them.

**The truth file is separate and never read by the engine.** It records what
should happen to every record, including the records whose correct outcome is
to remain unresolved. The engine sees only the CSVs.
"""
from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from datagen.scenarios import CATALOGUE, Disposition, Scenario
from finctl.engine.feemodel import DEFAULT_RATE_CARD, fee_breakdown
from finctl.money import format_inr, paise_to_rupees
from finctl.timeutil import IST, add_banking_days, is_banking_day

# Simulated banks, each with its own export dialect. The point is that the
# ingest layer must cope with all of them without being told which is which.
BANK_DIALECTS: tuple[dict[str, Any], ...] = (
    {
        "name": "HDFC",
        "date_format": "%d/%m/%Y",
        "utr_prefix": "HDFCN",
        "narration": "NEFT-{utr}-RAZORPAY SOFTWARE PRIVATE LIMITED-{acct}",
        "amount_style": "grouped",
    },
    {
        "name": "ICICI",
        "date_format": "%d-%b-%Y",
        "utr_prefix": "ICICR",
        "narration": "MMT/IMPS/{utr}/Settlement/RAZORPAY/{acct}",
        "amount_style": "plain",
    },
    {
        "name": "AXIS",
        "date_format": "%Y-%m-%d",
        "utr_prefix": "AXISP",
        "narration": "BY TRANSFER-NEFT*{ifsc}*{utr}*RAZORPAY SOFTWARE",
        "amount_style": "grouped",
    },
    {
        "name": "SBI",
        "date_format": "%d-%m-%Y",
        "utr_prefix": "SBINR",
        "narration": "TRANSFER FROM {utr} RAZORPAY SOFT PVT LTD REF {acct}",
        "amount_style": "plain",
    },
)

PAYMENT_METHODS: tuple[tuple[str, int], ...] = (
    ("upi", 42), ("credit_card", 18), ("debit_card", 14), ("netbanking", 12),
    ("wallet", 6), ("emi", 4), ("amex", 2), ("rupay_debit", 2),
)

CUSTOMERS: tuple[str, ...] = (
    "Aarav Traders", "Bhavna Retail", "Chetan Logistics", "Divya Foods",
    "Eshan Systems", "Falguni Textiles", "Gaurav Motors", "Hiral Pharma",
    "Ishaan Digital", "Jyoti Interiors", "Kabir Exports", "Lavanya Studio",
    "Manish Hardware", "Neha Organics", "Omkar Steel", "Pooja Ventures",
)


@dataclass(slots=True)
class TruthRecord:
    """What a correct engine should conclude about one generated case."""

    case_id: str
    scenario: str
    disposition: str
    expected_reason: str
    difficulty: str
    settlement_ids: list[str] = field(default_factory=list)
    bank_line_ids: list[str] = field(default_factory=list)
    payment_ids: list[str] = field(default_factory=list)
    record_ids: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "scenario": self.scenario,
            "disposition": self.disposition,
            "expected_reason": self.expected_reason,
            "difficulty": self.difficulty,
            "settlement_ids": sorted(self.settlement_ids),
            "bank_line_ids": sorted(self.bank_line_ids),
            "payment_ids": sorted(self.payment_ids),
            "record_ids": sorted(self.record_ids),
            "note": self.note,
        }


@dataclass(slots=True)
class World:
    """Every row the generator produced, before serialisation."""

    payments: list[dict[str, Any]] = field(default_factory=list)
    settlements: list[dict[str, Any]] = field(default_factory=list)
    refunds: list[dict[str, Any]] = field(default_factory=list)
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    bank_lines: list[dict[str, Any]] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    truth: list[TruthRecord] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return (
            len(self.payments) + len(self.settlements) + len(self.refunds)
            + len(self.adjustments) + len(self.bank_lines) + len(self.ledger)
        )


class Generator:
    """Deterministic generator. The same seed always produces the same month.

    Determinism is not a nicety here: it is what allows an accuracy figure to
    be reproduced and a regression to be attributed to an engine change rather
    than to a different roll of the dice.
    """

    def __init__(
        self,
        *,
        seed: int = 20260828,
        cases: int = 900,
        period_start: date = date(2026, 7, 1),
        period_days: int = 31,
    ) -> None:
        self.rng = random.Random(seed)
        self.cases = cases
        self.period_start = period_start
        self.period_days = period_days
        self.world = World()
        self._counter = 0
        self._used_utrs: set[str] = set()
        # Running bank balance, so the statement's balance column is internally
        # consistent -- an auditor's first sanity check on any statement.
        self._balance = 4_50_00_000

    # -- primitives -------------------------------------------------------

    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:06d}"

    def _weighted(self, options: Sequence[tuple[Any, int]]) -> Any:
        total = sum(weight for _, weight in options)
        roll = self.rng.randrange(total)
        cumulative = 0
        for value, weight in options:
            cumulative += weight
            if roll < cumulative:
                return value
        return options[-1][0]

    def _capture_date(self) -> date:
        """A capture date inside the period, weighted toward banking days."""
        for _ in range(12):
            day = self.period_start + timedelta(days=self.rng.randrange(self.period_days))
            if is_banking_day(day) or self.rng.random() < 0.25:
                return day
        return self.period_start

    def _make_utr(self, dialect: dict[str, Any]) -> str:
        """A rail-shaped UTR, unique unless a scenario deliberately reuses one."""
        while True:
            utr = dialect["utr_prefix"] + "".join(
                str(self.rng.randrange(10)) for _ in range(11)
            )
            if utr not in self._used_utrs:
                self._used_utrs.add(utr)
                return utr

    def _format_amount(self, paise: int, style: str) -> str:
        """Render an amount the way the simulated bank's export would."""
        if style == "grouped":
            return format_inr(paise, symbol=False)
        return str(paise_to_rupees(paise))

    def _narration(self, dialect: dict[str, Any], utr: str) -> str:
        return dialect["narration"].format(
            utr=utr,
            acct=f"{self.rng.randrange(10**8, 10**9)}",
            ifsc=f"{dialect['utr_prefix'][:4]}000{self.rng.randrange(1000, 9999)}",
        )

    # -- record builders --------------------------------------------------

    def _add_payment(
        self,
        *,
        captured_on: date,
        method: str | None = None,
        gross: int | None = None,
        settlement_id: str | None = None,
        international: bool = False,
        capture_time: datetime | None = None,
        fee_override: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        method = method or self._weighted(PAYMENT_METHODS)
        if gross is None:
            # Log-ish distribution: many small tickets, a few large ones. A
            # uniform distribution would make amount collisions unrealistically
            # rare and flatter the matcher.
            magnitude = self.rng.choice([2, 2, 2, 3, 3, 3, 3, 4, 4, 5])
            gross = self.rng.randrange(10 ** magnitude, 10 ** (magnitude + 1)) * 100

        if fee_override is not None:
            fee, tax = fee_override
        else:
            fee, tax = fee_breakdown(gross, "international" if international else method)

        moment = capture_time or datetime(
            captured_on.year, captured_on.month, captured_on.day,
            self.rng.randrange(7, 23), self.rng.randrange(60), tzinfo=IST,
        )

        payment = {
            "payment_id": self._next("pay"),
            "order_id": self._next("ord"),
            "gross_amount": str(paise_to_rupees(gross)),
            "fee": str(paise_to_rupees(fee)),
            "tax": str(paise_to_rupees(tax)),
            "net_amount": str(paise_to_rupees(gross - fee - tax)),
            "captured_at": moment.isoformat(),
            "method": "international" if international else method,
            "currency": "INR",
            "settlement_id": settlement_id or "",
            "international": "true" if international else "false",
            "source_currency": "USD" if international else "",
            "fx_rate": f"{self.rng.uniform(82.0, 87.5):.4f}" if international else "",
        }
        self.world.payments.append(payment)
        return payment

    def _add_ledger(self, payment: dict[str, Any], *, customer: str | None = None) -> None:
        self.world.ledger.append({
            "entry_id": self._next("led"),
            "order_id": payment["order_id"],
            "expected_gross": payment["gross_amount"],
            "customer": customer or self.rng.choice(CUSTOMERS),
            "booked_on": payment["captured_at"][:10],
            "currency": "INR",
            "status": "open",
        })

    def _add_settlement(
        self, *, utr: str | None, amount: int, settled_on: date,
        payment_ids: Iterable[str], refund_ids: Iterable[str] = (),
        adjustment_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        settlement = {
            "settlement_id": self._next("setl"),
            "utr": utr or "",
            "amount": str(paise_to_rupees(amount)),
            "settled_at": datetime(
                settled_on.year, settled_on.month, settled_on.day, 11, 30, tzinfo=IST
            ).isoformat(),
            "payment_ids": "|".join(payment_ids),
            "refund_ids": "|".join(refund_ids),
            "adjustment_ids": "|".join(adjustment_ids),
            "currency": "INR",
        }
        self.world.settlements.append(settlement)
        return settlement

    def _add_bank_line(
        self, *, dialect: dict[str, Any], value_date: date, amount: int,
        narration: str,
    ) -> dict[str, Any]:
        self._balance += amount
        line = {
            "line_id": self._next("bank"),
            "value_date": value_date.strftime(dialect["date_format"]),
            "narration": narration,
            "amount": self._format_amount(amount, dialect["amount_style"]),
            "balance": self._format_amount(self._balance, dialect["amount_style"]),
            "bank_ref": f"{dialect['name']}{self.rng.randrange(10**6, 10**7)}",
            "currency": "INR",
        }
        self.world.bank_lines.append(line)
        return line

    # -- the scenario builders -------------------------------------------

    def _simple_case(
        self, dialect: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], date]:
        """The common spine: some payments, one settlement, matching totals.

        Every scenario starts here and then breaks exactly one thing, which is
        what keeps a case attributable to a single cause.
        """
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        count = self.rng.choice([1, 1, 1, 2, 2, 3])
        payments = [self._add_payment(captured_on=captured) for _ in range(count)]
        net_total = sum(
            int(Decimal(p["net_amount"]) * 100) for p in payments
        )
        utr = self._make_utr(dialect)
        settlement = self._add_settlement(
            utr=utr, amount=net_total, settled_on=settled,
            payment_ids=[p["payment_id"] for p in payments],
        )
        for payment in payments:
            payment["settlement_id"] = settlement["settlement_id"]
            self._add_ledger(payment)
        return settlement, dialect, payments, settled

    def _emit(self, scenario: Scenario) -> None:
        """Build one case for a scenario and record its expected outcome."""
        dialect = self.rng.choice(BANK_DIALECTS)
        case_id = self._next("case")
        truth = TruthRecord(
            case_id=case_id,
            scenario=scenario.key,
            disposition=scenario.disposition.value,
            expected_reason=scenario.expected_reason,
            difficulty=scenario.difficulty,
        )
        handler = getattr(self, f"_case_{scenario.key}")
        handler(dialect, truth)
        self.world.truth.append(truth)

    # Each handler mutates `truth` with the ids it created. Keeping them
    # separate rather than parameterising one giant function means a scenario
    # can be read, changed, or debugged in isolation.

    def _case_clean_exact(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled,
            amount=int(Decimal(settlement["amount"]) * 100),
            narration=self._narration(dialect, settlement["utr"]),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]

    def _case_utr_noisy_narration(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        noisy = (
            f"{self.rng.choice(['CMS', 'INB', 'ACH C-', 'BY TRF'])} "
            f"{self._narration(dialect, settlement['utr'])} "
            f"BAL {self.rng.randrange(10**6, 10**8)} "
            f"{settled.strftime('%d%m%Y')}"
        )
        line = self._add_bank_line(
            dialect=dialect, value_date=settled,
            amount=int(Decimal(settlement["amount"]) * 100), narration=noisy,
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]

    def _case_utr_truncated(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        full = self._narration(dialect, settlement["utr"])
        # Clip so that between 9 and 13 characters of the UTR survive -- the
        # realistic outcome of a 40-character field limit.
        cut = full.index(settlement["utr"]) + self.rng.randrange(9, 14)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled,
            amount=int(Decimal(settlement["amount"]) * 100), narration=full[:cut],
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = "narration clipped mid-UTR"

    def _case_utr_typo(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        utr = settlement["utr"]
        position = self.rng.randrange(6, len(utr) - 2)
        corrupted = utr[:position] + utr[position + 1] + utr[position] + utr[position + 2:]
        line = self._add_bank_line(
            dialect=dialect, value_date=settled,
            amount=int(Decimal(settlement["amount"]) * 100),
            narration=self._narration(dialect, corrupted),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = f"UTR transposed at position {position}"

    def _case_no_utr_unique_amount(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled,
            amount=int(Decimal(settlement["amount"]) * 100),
            narration=f"NEFT CR RAZORPAY SOFTWARE PVT LTD {settled.strftime('%d%m%y')}",
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]

    def _case_rounding_drift(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        drift = self.rng.choice([-2, -1, 1, 2])
        line = self._add_bank_line(
            dialect=dialect, value_date=settled,
            amount=int(Decimal(settlement["amount"]) * 100) + drift,
            narration=self._narration(dialect, settlement["utr"]),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = f"{drift:+d} paise drift"

    def _case_fee_recovered(self, dialect, truth) -> None:
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        method = self.rng.choice(["credit_card", "netbanking", "wallet", "emi"])
        payment = self._add_payment(captured_on=captured, method=method)
        net = int(Decimal(payment["net_amount"]) * 100)
        settlement = self._add_settlement(
            utr=None, amount=net, settled_on=settled,
            payment_ids=[payment["payment_id"]],
        )
        payment["settlement_id"] = settlement["settlement_id"]
        self._add_ledger(payment)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=net,
            narration="NEFT INWARD RAZORPAY SOFTWARE PRIVATE LIMITED",
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [payment["payment_id"]]
        truth.note = f"gross recoverable only by inverting the {method} rate card"

    def _case_batched_credit(self, dialect, truth) -> None:
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        batch_size = self.rng.randrange(3, 7)
        settlements = []
        for _ in range(batch_size):
            payments = [self._add_payment(captured_on=captured)
                        for _ in range(self.rng.choice([1, 1, 2]))]
            net = sum(int(Decimal(p["net_amount"]) * 100) for p in payments)
            settlement = self._add_settlement(
                utr=None, amount=net, settled_on=settled,
                payment_ids=[p["payment_id"] for p in payments],
            )
            for payment in payments:
                payment["settlement_id"] = settlement["settlement_id"]
                self._add_ledger(payment)
                truth.payment_ids.append(payment["payment_id"])
            settlements.append(settlement)

        total = sum(int(Decimal(s["amount"]) * 100) for s in settlements)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=total,
            narration=f"RTGS CONSOLIDATED PAYOUT RAZORPAY {batch_size} BATCHES",
        )
        truth.settlement_ids = [s["settlement_id"] for s in settlements]
        truth.bank_line_ids = [line["line_id"]]
        truth.note = f"{batch_size} settlements netted into one credit"

    def _case_split_settlement(self, dialect, truth) -> None:
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        payments = [self._add_payment(captured_on=captured, gross=self.rng.randrange(
            50_000_00, 200_000_00)) for _ in range(2)]
        net = sum(int(Decimal(p["net_amount"]) * 100) for p in payments)
        utr = self._make_utr(dialect)
        settlement = self._add_settlement(
            utr=utr, amount=net, settled_on=settled,
            payment_ids=[p["payment_id"] for p in payments],
        )
        for payment in payments:
            payment["settlement_id"] = settlement["settlement_id"]
            self._add_ledger(payment)

        first = net // 2
        line_a = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=first,
            narration=f"RTGS-{utr}-RAZORPAY PART 1 OF 2",
        )
        line_b = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=net - first,
            narration=f"RTGS-{utr}-RAZORPAY PART 2 OF 2",
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line_a["line_id"], line_b["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = "single payout split across two credits"

    def _case_late_credit(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        line = self._add_bank_line(
            dialect=dialect, value_date=add_banking_days(settled, 1),
            amount=int(Decimal(settlement["amount"]) * 100),
            narration=self._narration(dialect, settlement["utr"]),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]

    def _case_holiday_slip(self, dialect, truth) -> None:
        # Captured two banking days before Diwali so that T+2 lands on it.
        captured = date(2026, 11, 5)
        settled = add_banking_days(captured, 2)
        payments = [self._add_payment(captured_on=captured)]
        net = sum(int(Decimal(p["net_amount"]) * 100) for p in payments)
        utr = self._make_utr(dialect)
        settlement = self._add_settlement(
            utr=utr, amount=net, settled_on=settled,
            payment_ids=[p["payment_id"] for p in payments],
        )
        for payment in payments:
            payment["settlement_id"] = settlement["settlement_id"]
            self._add_ledger(payment)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=net,
            narration=self._narration(dialect, utr),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = "T+2 spans the Diwali bank holiday"

    def _case_timezone_boundary(self, dialect, truth) -> None:
        captured = self._capture_date()
        # 23:45 UTC on day D is 05:15 IST on day D+1. The settlement cycle runs
        # off the IST date, so an engine that buckets on the UTC date puts this
        # payment in the previous day's batch and never matches it.
        moment_utc = datetime(
            captured.year, captured.month, captured.day, 23, 45, tzinfo=timezone.utc
        )
        ist_date = moment_utc.astimezone(IST).date()
        settled = add_banking_days(ist_date, 2)

        payment = self._add_payment(
            captured_on=ist_date, capture_time=moment_utc.astimezone(IST)
        )
        # Stamped in UTC with a Z suffix, exactly as a PSP API returns it, so
        # the ingest layer has to do the conversion itself
        payment["captured_at"] = moment_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        net = int(Decimal(payment["net_amount"]) * 100)
        utr = self._make_utr(dialect)
        settlement = self._add_settlement(
            utr=utr, amount=net, settled_on=settled,
            payment_ids=[payment["payment_id"]],
        )
        payment["settlement_id"] = settlement["settlement_id"]
        self._add_ledger(payment)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=net,
            narration=self._narration(dialect, utr),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [payment["payment_id"]]
        truth.note = "capture timestamp supplied in UTC, crosses IST midnight"

    def _case_very_late_credit(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        line = self._add_bank_line(
            dialect=dialect, value_date=add_banking_days(settled, 9),
            amount=int(Decimal(settlement["amount"]) * 100),
            narration=self._narration(dialect, settlement["utr"]),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = "9 banking days late; SLA breach must surface, not be absorbed"

    def _case_refund_netted(self, dialect, truth) -> None:
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        payments = [self._add_payment(captured_on=captured) for _ in range(3)]
        refunded = payments[0]
        # Capped below the cycle's net so the payout stays a credit; a payout
        # driven negative by refunds is the separate negative-settlement case.
        net_available = sum(int(Decimal(p["net_amount"]) * 100) for p in payments)
        refund_amount = min(
            int(Decimal(refunded["gross_amount"]) * 100),
            max(1_00, int(net_available * 0.6)),
        )
        refund = {
            "refund_id": self._next("rfnd"),
            "payment_id": refunded["payment_id"],
            "amount": str(paise_to_rupees(refund_amount)),
            "created_at": datetime(
                settled.year, settled.month, settled.day, 9, 15, tzinfo=IST
            ).isoformat(),
            "settlement_id": "",
        }
        self.world.refunds.append(refund)

        net = sum(int(Decimal(p["net_amount"]) * 100) for p in payments) - refund_amount
        utr = self._make_utr(dialect)
        settlement = self._add_settlement(
            utr=utr, amount=net, settled_on=settled,
            payment_ids=[p["payment_id"] for p in payments],
            refund_ids=[refund["refund_id"]],
        )
        refund["settlement_id"] = settlement["settlement_id"]
        for payment in payments:
            payment["settlement_id"] = settlement["settlement_id"]
            self._add_ledger(payment)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=net,
            narration=self._narration(dialect, utr),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = "payout reduced by a refund issued in-cycle"

    def _case_chargeback_deduction(self, dialect, truth) -> None:
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        payments = [self._add_payment(captured_on=captured) for _ in range(3)]
        disputed = payments[1]
        net_available = sum(int(Decimal(p["net_amount"]) * 100) for p in payments)
        dispute_fee = 750_00
        claw_back = min(
            int(Decimal(disputed["gross_amount"]) * 100),
            max(1_00, int(net_available * 0.5) - dispute_fee),
        )
        adjustment = {
            "adjustment_id": self._next("adj"),
            "kind": "chargeback",
            "amount": str(paise_to_rupees(-(claw_back + dispute_fee))),
            "created_at": datetime(
                settled.year, settled.month, settled.day, 8, 0, tzinfo=IST
            ).isoformat(),
            "settlement_id": "",
            "linked_payment_id": disputed["payment_id"],
        }
        self.world.adjustments.append(adjustment)

        net = sum(int(Decimal(p["net_amount"]) * 100) for p in payments) - claw_back - dispute_fee
        utr = self._make_utr(dialect)
        settlement = self._add_settlement(
            utr=utr, amount=net, settled_on=settled,
            payment_ids=[p["payment_id"] for p in payments],
            adjustment_ids=[adjustment["adjustment_id"]],
        )
        adjustment["settlement_id"] = settlement["settlement_id"]
        for payment in payments:
            payment["settlement_id"] = settlement["settlement_id"]
            self._add_ledger(payment)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=net,
            narration=self._narration(dialect, utr),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = "chargeback plus dispute fee deducted from the payout"

    def _case_unlinked_chargeback(self, dialect, truth) -> None:
        settled = self._capture_date()
        amount = self.rng.randrange(5_000_00, 60_000_00)
        adjustment = {
            "adjustment_id": self._next("adj"),
            "kind": "chargeback",
            "amount": str(paise_to_rupees(-amount)),
            "created_at": datetime(
                settled.year, settled.month, settled.day, 8, 0, tzinfo=IST
            ).isoformat(),
            "settlement_id": "",
            # Points at a payment from a prior period, absent from this batch.
            "linked_payment_id": f"pay_{self.rng.randrange(1, 900):06d}_prior",
        }
        self.world.adjustments.append(adjustment)
        truth.record_ids = [adjustment["adjustment_id"]]
        truth.note = "deduction references a payment outside the period"

    def _case_unlinked_refund(self, dialect, truth) -> None:
        settled = self._capture_date()
        amount = self.rng.randrange(2_000_00, 40_000_00)
        refund = {
            "refund_id": self._next("rfnd"),
            "payment_id": f"pay_{self.rng.randrange(1, 900):06d}_prior",
            "amount": str(paise_to_rupees(amount)),
            "created_at": datetime(
                settled.year, settled.month, settled.day, 10, 0, tzinfo=IST
            ).isoformat(),
            "settlement_id": "",
        }
        self.world.refunds.append(refund)
        truth.record_ids = [refund["refund_id"]]
        truth.note = "refund references a payment outside the period"

    def _case_missing_bank_credit(self, dialect, truth) -> None:
        settlement, _, payments, _ = self._simple_case(dialect)
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = "no bank line generated at all"

    def _case_unexpected_credit(self, dialect, truth) -> None:
        value_date = self._capture_date()
        amount = self.rng.randrange(10_000_00, 500_000_00)
        narration = self.rng.choice([
            f"NEFT-{self.rng.choice(CUSTOMERS).upper().replace(' ', '')}-DIRECT PAYMENT",
            "INTEREST CREDIT SAVINGS ACCOUNT QUARTERLY",
            f"RTGS INWARD WORKING CAPITAL DISBURSAL {self.rng.randrange(10**8, 10**9)}",
            "INTER ACCOUNT SWEEP FROM OD ACCOUNT",
        ])
        line = self._add_bank_line(
            dialect=dialect, value_date=value_date, amount=amount, narration=narration,
        )
        truth.bank_line_ids = [line["line_id"]]
        truth.note = "credit that no settlement explains"

    def _case_amount_mismatch(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        payout = int(Decimal(settlement["amount"]) * 100)
        # Proportional, so the shortfall is material without ever exceeding the
        # payout itself -- a credit that goes negative is a different scenario.
        shortfall = max(1_00, int(payout * self.rng.uniform(0.04, 0.25)))
        line = self._add_bank_line(
            dialect=dialect, value_date=settled,
            amount=payout - shortfall,
            narration=self._narration(dialect, settlement["utr"]),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = f"credit short by {format_inr(shortfall)} with no documented cause"

    def _case_same_amount_ambiguity(self, dialect, truth) -> None:
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        gross = self.rng.randrange(20_000_00, 90_000_00)
        settlements = []
        for _ in range(2):
            payment = self._add_payment(
                captured_on=captured, method="upi", gross=gross
            )
            net = int(Decimal(payment["net_amount"]) * 100)
            settlement = self._add_settlement(
                utr=None, amount=net, settled_on=settled,
                payment_ids=[payment["payment_id"]],
            )
            payment["settlement_id"] = settlement["settlement_id"]
            self._add_ledger(payment)
            settlements.append(settlement)
            truth.payment_ids.append(payment["payment_id"])

        line = self._add_bank_line(
            dialect=dialect, value_date=settled,
            amount=int(Decimal(settlements[0]["amount"]) * 100),
            narration="NEFT CR RAZORPAY SOFTWARE PVT LTD",
        )
        truth.settlement_ids = [s["settlement_id"] for s in settlements]
        truth.bank_line_ids = [line["line_id"]]
        truth.note = "two identical payouts, no reference on either; genuinely undecidable"

    def _case_scale_error(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled,
            amount=int(Decimal(settlement["amount"]) * 100) // 100,
            narration=self._narration(dialect, settlement["utr"]),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = "bank amount is 100x too small -- paise read as rupees"

    def _case_transposition(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        original = int(Decimal(settlement["amount"]) * 100)
        digits = list(str(original))

        # Swapping two equal digits leaves the amount untouched, which would
        # label a perfectly clean settlement as a break and score a correct
        # match as a failure. Search for a swap that actually changes the
        # value, and fall back to a clean case when the amount has no distinct
        # adjacent pair -- the generator must never assert a break it did not
        # actually create.
        swapped = original
        positions = list(range(len(digits) - 1))
        self.rng.shuffle(positions)
        for position in positions:
            if digits[position] == digits[position + 1]:
                continue
            trial = digits[:]
            trial[position], trial[position + 1] = trial[position + 1], trial[position]
            candidate = int("".join(trial))
            if candidate != original and candidate > 0:
                swapped = candidate
                break

        if swapped == original:
            self._case_clean_exact(dialect, truth)
            truth.scenario = "clean_exact"
            truth.disposition = "match"
            truth.expected_reason = "utr_exact"
            truth.difficulty = "trivial"
            truth.note = "no distinct adjacent digits to transpose; emitted clean"
            return

        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=swapped,
            narration=self._narration(dialect, settlement["utr"]),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = (
            f"amount keyed as {format_inr(swapped)} against a payout of "
            f"{format_inr(original)} -- two digits swapped"
        )

    def _case_duplicate_utr(self, dialect, truth) -> None:
        settlement, _, payments, settled = self._simple_case(dialect)
        amount = int(Decimal(settlement["amount"]) * 100)
        narration = self._narration(dialect, settlement["utr"])
        first = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=amount, narration=narration,
        )
        second = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=amount, narration=narration,
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [first["line_id"], second["line_id"]]
        truth.payment_ids = [p["payment_id"] for p in payments]
        truth.note = "the same UTR credited twice; one is a double-post"

    def _case_fee_overcharge(self, dialect, truth) -> None:
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        method = self.rng.choice(["credit_card", "netbanking", "wallet"])
        gross = self.rng.randrange(20_000_00, 200_000_00)
        correct_fee, correct_tax = fee_breakdown(gross, method)
        # An overcharge of 10-40 bps, the size a merchant actually disputes.
        excess = int(gross * self.rng.randrange(10, 41) / 10_000)
        payment = self._add_payment(
            captured_on=captured, method=method, gross=gross,
            fee_override=(correct_fee + excess, correct_tax),
        )
        net = int(Decimal(payment["net_amount"]) * 100)
        utr = self._make_utr(dialect)
        settlement = self._add_settlement(
            utr=utr, amount=net, settled_on=settled,
            payment_ids=[payment["payment_id"]],
        )
        payment["settlement_id"] = settlement["settlement_id"]
        self._add_ledger(payment)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=net,
            narration=self._narration(dialect, utr),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [payment["payment_id"]]
        truth.note = f"commission {format_inr(excess)} above the {method} rate card"

    def _case_fx_unverified(self, dialect, truth) -> None:
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        payment = self._add_payment(captured_on=captured, international=True)
        net = int(Decimal(payment["net_amount"]) * 100)
        utr = self._make_utr(dialect)
        settlement = self._add_settlement(
            utr=utr, amount=net, settled_on=settled,
            payment_ids=[payment["payment_id"]],
        )
        payment["settlement_id"] = settlement["settlement_id"]
        self._add_ledger(payment)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=net,
            narration=self._narration(dialect, utr),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [payment["payment_id"]]
        truth.note = "USD payment settled in INR; applied rate not independently checkable"

    def _case_ledger_missing(self, dialect, truth) -> None:
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        payment = self._add_payment(captured_on=captured)
        net = int(Decimal(payment["net_amount"]) * 100)
        utr = self._make_utr(dialect)
        settlement = self._add_settlement(
            utr=utr, amount=net, settled_on=settled,
            payment_ids=[payment["payment_id"]],
        )
        payment["settlement_id"] = settlement["settlement_id"]
        # Deliberately no ledger entry.
        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=net,
            narration=self._narration(dialect, utr),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [payment["payment_id"]]
        truth.note = "settled payment with no internal ledger entry"

    def _case_partial_on_hold(self, dialect, truth) -> None:
        captured = self._capture_date()
        settled = add_banking_days(captured, 2)
        payment = self._add_payment(
            captured_on=captured, gross=self.rng.randrange(100_000_00, 400_000_00)
        )
        net = int(Decimal(payment["net_amount"]) * 100)
        released = int(net * self.rng.uniform(0.55, 0.8))
        utr = self._make_utr(dialect)
        settlement = self._add_settlement(
            utr=utr, amount=released, settled_on=settled,
            payment_ids=[payment["payment_id"]],
        )
        payment["settlement_id"] = settlement["settlement_id"]
        self._add_ledger(payment)
        line = self._add_bank_line(
            dialect=dialect, value_date=settled, amount=released,
            narration=self._narration(dialect, utr),
        )
        truth.settlement_ids = [settlement["settlement_id"]]
        truth.bank_line_ids = [line["line_id"]]
        truth.payment_ids = [payment["payment_id"]]
        truth.note = f"{format_inr(net - released)} retained as reserve"

    def _case_malformed_amount(self, dialect, truth) -> None:
        value_date = self._capture_date()
        line = self._add_bank_line(
            dialect=dialect, value_date=value_date, amount=self.rng.randrange(1000, 99999),
            narration="NEFT CR RAZORPAY SOFTWARE PVT LTD",
        )
        line["amount"] = self.rng.choice(["", "#REF!", "N/A", "1,2E+05", "--"])
        truth.bank_line_ids = [line["line_id"]]
        truth.note = "corrupted amount field; must be quarantined, never coerced to zero"

    def _case_reversal_pair(self, dialect, truth) -> None:
        value_date = self._capture_date()
        amount = self.rng.randrange(20_000_00, 300_000_00)
        reference = f"RVSL{self.rng.randrange(10**9, 10**10)}"
        credit = self._add_bank_line(
            dialect=dialect, value_date=value_date, amount=amount,
            narration=f"NEFT INWARD {reference} RAZORPAY SOFTWARE",
        )
        debit = self._add_bank_line(
            dialect=dialect, value_date=value_date, amount=-amount,
            narration=f"NEFT RETURN {reference} BENEFICIARY ACCOUNT CLOSED",
        )
        truth.bank_line_ids = [credit["line_id"], debit["line_id"]]
        truth.note = "failed transfer returned same day; the pair nets to zero"

    # -- driver -----------------------------------------------------------

    def generate(self) -> World:
        """Produce the full batch, then shuffle each file's row order.

        Shuffling matters. Real exports are not grouped by case, and an engine
        that only works because related rows are adjacent has not solved
        anything. The shuffle uses the seeded RNG, so it stays reproducible.
        """
        weighted = [(scenario, scenario.weight) for scenario in CATALOGUE]
        for _ in range(self.cases):
            self._emit(self._weighted(weighted))

        for rows in (
            self.world.payments, self.world.settlements, self.world.refunds,
            self.world.adjustments, self.world.bank_lines, self.world.ledger,
        ):
            self.rng.shuffle(rows)

        return self.world


def write_world(world: World, out_dir: Path) -> dict[str, int]:
    """Serialise a world to CSVs plus a separate truth file.

    The truth file is written alongside but is never an input to the engine.
    Keeping it in the same directory is a convenience for the scorer; keeping
    it out of the ingest manifest is what keeps the measurement honest.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}

    datasets = (
        ("psp_payments.csv", world.payments),
        ("psp_settlements.csv", world.settlements),
        ("psp_refunds.csv", world.refunds),
        ("psp_adjustments.csv", world.adjustments),
        ("bank_statement.csv", world.bank_lines),
        ("ledger.csv", world.ledger),
    )
    for filename, rows in datasets:
        path = out_dir / filename
        if not rows:
            path.write_text("", encoding="utf-8")
            written[filename] = 0
            continue
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        written[filename] = len(rows)

    truth_path = out_dir / "truth.json"
    truth_path.write_text(
        json.dumps([record.as_dict() for record in world.truth], indent=2),
        encoding="utf-8",
    )
    written["truth.json"] = len(world.truth)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate a labelled settlement batch.")
    parser.add_argument("--out", type=Path, default=Path("data"))
    parser.add_argument("--cases", type=int, default=900)
    parser.add_argument("--seed", type=int, default=20260828)
    args = parser.parse_args(argv)

    generator = Generator(seed=args.seed, cases=args.cases)
    world = generator.generate()
    written = write_world(world, args.out)

    print(f"Generated {world.record_count:,} records across {args.cases:,} cases "
          f"(seed {args.seed})")
    for name, count in written.items():
        print(f"  {name:24} {count:>7,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
