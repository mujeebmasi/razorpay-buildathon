"""Turning messy exports into canonical records, or refusing to.

Two rules govern this layer.

**Header names are matched by alias, not by position.** Every finance team
renames columns, and a positional parser breaks silently the first time someone
inserts a column. Aliases are matched case- and separator-insensitively, so
"Value Date", "value_date" and "VALUE-DATE" are the same field.

**A row that cannot be parsed is quarantined, never coerced.** The tempting
alternative -- default a bad amount to zero and move on -- produces a run that
balances while being wrong, which is the single most dangerous outcome a
reconciliation system can have. Quarantined rows are excluded from every total
and surface as their own exception class, so the operator sees exactly what was
not considered.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from finctl.models import (
    Adjustment, BankLine, LedgerEntry, Payment, Refund, Settlement,
)
from finctl.money import MoneyParseError, parse_money
from finctl.timeutil import DateParseError, business_date, parse_datetime

_NORMALISE_HEADER = re.compile(r"[^a-z0-9]+")


def _canon(name: str) -> str:
    """Reduce a header to its comparable form: lowercase alphanumerics."""
    return _NORMALISE_HEADER.sub("", name.strip().lower())


@dataclass(slots=True)
class Quarantine:
    """A row that could not be parsed, kept with enough context to fix it."""

    source_file: str
    row_number: int
    reason: str
    raw: Mapping[str, Any]
    record_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "row_number": self.row_number,
            "reason": self.reason,
            "record_id": self.record_id,
            "raw": dict(self.raw),
        }


@dataclass(slots=True)
class Batch:
    """Everything successfully ingested, plus everything that was not."""

    payments: list[Payment] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    bank_lines: list[BankLine] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    quarantined: list[Quarantine] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return (
            len(self.payments) + len(self.settlements) + len(self.refunds)
            + len(self.adjustments) + len(self.bank_lines) + len(self.ledger)
        )

    def index_payments(self) -> dict[str, Payment]:
        return {p.payment_id: p for p in self.payments}

    def index_settlements(self) -> dict[str, Settlement]:
        return {s.settlement_id: s for s in self.settlements}

    def index_bank_lines(self) -> dict[str, BankLine]:
        return {b.line_id: b for b in self.bank_lines}


class _Row:
    """Alias-tolerant accessor over one CSV row.

    Wrapping the row rather than pre-renaming columns keeps the untouched
    original available for the quarantine record and the audit trail, which is
    what an operator needs in order to fix the export.
    """

    __slots__ = ("raw", "_by_canon")

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.raw = raw
        self._by_canon = {_canon(k): v for k, v in raw.items() if k}

    def get(self, *aliases: str, default: Any = None) -> Any:
        for alias in aliases:
            value = self._by_canon.get(_canon(alias))
            if value not in (None, ""):
                return value
        return default

    def text(self, *aliases: str, default: str = "") -> str:
        value = self.get(*aliases)
        return str(value).strip() if value is not None else default

    def flag(self, *aliases: str) -> bool:
        return self.text(*aliases).strip().lower() in {"true", "yes", "y", "1"}


def _read_csv(path: Path) -> Iterable[tuple[int, Mapping[str, Any]]]:
    """Yield (row_number, row) pairs, tolerating an empty or absent file.

    A missing source file is a legitimate state -- a merchant with no refunds
    this period -- and must not be an error. A file that exists but cannot be
    read is a different problem and is allowed to raise.
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for number, row in enumerate(csv.DictReader(handle), start=2):
            yield number, row


def _load_file(
    path: Path,
    builder: Callable[[_Row], Any],
    sink: list[Any],
    quarantined: list[Quarantine],
) -> None:
    """Parse one file, routing failures to quarantine instead of aborting.

    One malformed row must not cost the other 5,339. Only the parse errors this
    layer is designed to catch are absorbed; anything else propagates, because
    a TypeError here is a bug in the builder, not bad data.
    """
    for number, raw in _read_csv(path):
        row = _Row(raw)
        try:
            record = builder(row)
        except (MoneyParseError, DateParseError, ValueError, KeyError) as exc:
            quarantined.append(
                Quarantine(
                    source_file=path.name,
                    row_number=number,
                    reason=str(exc),
                    raw=dict(raw),
                    # Best effort: an unparseable amount does not stop us
                    # identifying *which* record needs fixing.
                    record_id=row.text("line_id", "id", "payment_id",
                                       "settlement_id", "entry_id"),
                )
            )
            continue
        if record is not None:
            sink.append(record)


def _split_ids(value: str) -> tuple[str, ...]:
    """Split a packed id list on any of the separators exports use."""
    if not value:
        return ()
    return tuple(part.strip() for part in re.split(r"[|,;]", value) if part.strip())


# -- builders -------------------------------------------------------------


def _build_payment(row: _Row) -> Payment:
    gross = parse_money(row.get("gross_amount", "amount", "gross"), field="gross_amount")
    fee = parse_money(row.get("fee", "commission", default="0"), field="fee")
    tax = parse_money(row.get("tax", "gst", default="0"), field="tax")

    net_raw = row.get("net_amount", "net", "settled_amount")
    net = parse_money(net_raw, field="net_amount") if net_raw is not None else gross - fee - tax

    # The stated net and the derived net must agree. When they do not, the
    # export is internally inconsistent and no downstream arithmetic can be
    # trusted, so the row is rejected rather than silently reconciled to one
    # side or the other.
    if net_raw is not None and net != gross - fee - tax:
        raise ValueError(
            f"payment does not balance: net {net} != gross {gross} - fee {fee} - tax {tax}"
        )

    payment_id = row.text("payment_id", "id", "txn_id")
    if not payment_id:
        raise ValueError("payment_id is missing")

    return Payment(
        payment_id=payment_id,
        order_id=row.text("order_id", "order", "reference_id"),
        gross=gross,
        fee=fee,
        tax=tax,
        net=net,
        captured_at=parse_datetime(
            row.get("captured_at", "created_at", "transaction_date"), field="captured_at"
        ),
        method=row.text("method", "payment_method", default="unknown").lower(),
        currency=row.text("currency", default="INR").upper(),
        settlement_id=row.text("settlement_id") or None,
        international=row.flag("international", "is_international"),
        fx_rate=row.text("fx_rate") or None,
        source_currency=row.text("source_currency") or None,
        raw=dict(row.raw),
    )


def _build_settlement(row: _Row) -> Settlement:
    settlement_id = row.text("settlement_id", "id", "payout_id")
    if not settlement_id:
        raise ValueError("settlement_id is missing")
    return Settlement(
        settlement_id=settlement_id,
        utr=row.text("utr", "reference", "rrn") or None,
        amount=parse_money(row.get("amount", "net_amount", "payout_amount"), field="amount"),
        settled_at=parse_datetime(
            row.get("settled_at", "settlement_date", "date"), field="settled_at"
        ),
        payment_ids=_split_ids(row.text("payment_ids")),
        refund_ids=_split_ids(row.text("refund_ids")),
        adjustment_ids=_split_ids(row.text("adjustment_ids")),
        currency=row.text("currency", default="INR").upper(),
        raw=dict(row.raw),
    )


def _build_refund(row: _Row) -> Refund:
    return Refund(
        refund_id=row.text("refund_id", "id"),
        payment_id=row.text("payment_id"),
        amount=parse_money(row.get("amount"), field="amount"),
        created_at=parse_datetime(row.get("created_at", "date"), field="created_at"),
        settlement_id=row.text("settlement_id") or None,
        raw=dict(row.raw),
    )


def _build_adjustment(row: _Row) -> Adjustment:
    return Adjustment(
        adjustment_id=row.text("adjustment_id", "id"),
        kind=row.text("kind", "type", default="correction").lower(),
        amount=parse_money(row.get("amount"), field="amount"),
        created_at=parse_datetime(row.get("created_at", "date"), field="created_at"),
        settlement_id=row.text("settlement_id") or None,
        linked_payment_id=row.text("linked_payment_id", "payment_id") or None,
        raw=dict(row.raw),
    )


def _build_bank_line(row: _Row) -> BankLine:
    line_id = row.text("line_id", "id", "txn_id")
    if not line_id:
        raise ValueError("line_id is missing")

    amount_raw = row.get("amount", "credit", "deposit", "transaction_amount")
    if amount_raw is None:
        # Some statements use separate debit and credit columns rather than one
        # signed column. Reconstruct the signed amount from whichever is set.
        credit = row.get("credit_amount", "cr")
        debit = row.get("debit_amount", "dr")
        if credit is not None:
            amount = parse_money(credit, field="credit_amount")
        elif debit is not None:
            amount = -abs(parse_money(debit, field="debit_amount"))
        else:
            raise ValueError("no amount, credit or debit column found")
    else:
        amount = parse_money(amount_raw, field="amount")

    balance_raw = row.get("balance", "running_balance", "closing_balance")
    try:
        balance = parse_money(balance_raw, field="balance") if balance_raw else None
    except MoneyParseError:
        # A malformed balance is cosmetic; it never enters the arithmetic, so
        # it must not cost us an otherwise-good statement line.
        balance = None

    return BankLine(
        line_id=line_id,
        value_date=business_date(
            row.get("value_date", "date", "txn_date", "posting_date"), field="value_date"
        ),
        narration=row.text("narration", "description", "particulars", "remarks"),
        amount=amount,
        balance=balance,
        bank_ref=row.text("bank_ref", "cheque_no", "ref_no") or None,
        currency=row.text("currency", default="INR").upper(),
        raw=dict(row.raw),
    )


def _build_ledger_entry(row: _Row) -> LedgerEntry:
    return LedgerEntry(
        entry_id=row.text("entry_id", "id"),
        order_id=row.text("order_id", "order", "reference"),
        expected_gross=parse_money(
            row.get("expected_gross", "amount", "invoice_amount"), field="expected_gross"
        ),
        customer=row.text("customer", "party", "counterparty"),
        booked_on=business_date(row.get("booked_on", "date", "invoice_date"), field="booked_on"),
        currency=row.text("currency", default="INR").upper(),
        status=row.text("status", default="open").lower(),
        raw=dict(row.raw),
    )


#: Which file feeds which collection. Declared as data so a deployment can
#: point at differently-named exports without touching the loading logic.
DEFAULT_MANIFEST: Mapping[str, tuple[str, Callable[[_Row], Any]]] = {
    "psp_payments.csv": ("payments", _build_payment),
    "psp_settlements.csv": ("settlements", _build_settlement),
    "psp_refunds.csv": ("refunds", _build_refund),
    "psp_adjustments.csv": ("adjustments", _build_adjustment),
    "bank_statement.csv": ("bank_lines", _build_bank_line),
    "ledger.csv": ("ledger", _build_ledger_entry),
}


def load_batch(
    data_dir: Path,
    manifest: Mapping[str, tuple[str, Callable[[_Row], Any]]] = DEFAULT_MANIFEST,
) -> Batch:
    """Load every source file in the manifest into one canonical batch.

    Note what is absent: the truth file. The engine never sees the labels, and
    keeping that boundary in the loader rather than by convention is what makes
    the accuracy measurement mean something.
    """
    batch = Batch()
    for filename, (attribute, builder) in manifest.items():
        _load_file(
            data_dir / filename,
            builder,
            getattr(batch, attribute),
            batch.quarantined,
        )
    return batch


def load_truth(data_dir: Path) -> list[dict[str, Any]]:
    """Load the held-out labels. Only the scorer may call this."""
    import json

    path = data_dir / "truth.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))
