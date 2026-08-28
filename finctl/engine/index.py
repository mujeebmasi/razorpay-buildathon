"""Candidate generation.

Matching is quadratic if written naively: every settlement against every bank
line. At 1,000 settlements and 900 lines that is a million narration parses,
and it grows as the square of the merchant's volume, which is exactly the wrong
shape for a system whose selling point is throughput.

These indexes reduce it to near-linear by answering "which handful of lines
could possibly relate to this settlement?" three different ways -- by exact
reference, by reference prefix (which survives truncation), and by date window
-- and taking the union. Expensive fuzzy comparison then runs only over that
union, which is tens of records rather than hundreds.

Everything here is built once per run and read-only afterwards.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Sequence

from finctl.engine.narration import (
    RefCandidate, bounded_edit_distance, extract_candidates, normalize_ref,
)
from finctl.models import BankLine, Settlement

# How many leading characters key the truncation bucket. Eight is long enough
# that a bucket stays small on a real statement, and short enough that a
# narration clipped mid-reference still lands in the right one.
_PREFIX_KEY_LENGTH = 8


@dataclass(slots=True)
class LineFacts:
    """Everything derived from a bank line once, so no pass recomputes it."""

    line: BankLine
    flattened: str
    candidates: tuple[RefCandidate, ...]
    tokens: frozenset[str]

    @property
    def line_id(self) -> str:
        return self.line.line_id


@dataclass(slots=True)
class BankIndex:
    """Bank statement lines, indexed for candidate lookup."""

    facts: dict[str, LineFacts] = field(default_factory=dict)
    by_token: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_prefix: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_date: dict[date, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_amount: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    credit_ids: set[str] = field(default_factory=set)
    debit_ids: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, lines: Iterable[BankLine]) -> "BankIndex":
        index = cls()
        for line in lines:
            candidates = tuple(extract_candidates(line.narration))
            tokens = frozenset(c.token for c in candidates)
            index.facts[line.line_id] = LineFacts(
                line=line,
                flattened=normalize_ref(line.narration),
                candidates=candidates,
                tokens=tokens,
            )
            for token in tokens:
                index.by_token[token].add(line.line_id)
                if len(token) >= _PREFIX_KEY_LENGTH:
                    index.by_prefix[token[:_PREFIX_KEY_LENGTH]].add(line.line_id)
            index.by_date[line.value_date].add(line.line_id)
            index.by_amount[line.amount].add(line.line_id)
            (index.credit_ids if line.is_credit else index.debit_ids).add(line.line_id)
        return index

    def in_window(self, start: date, end: date) -> set[str]:
        """Line ids whose value date falls in [start, end]."""
        found: set[str] = set()
        current = start
        while current <= end:
            found |= self.by_date.get(current, set())
            current += timedelta(days=1)
        return found

    def reference_candidates(self, utr: str, window: set[str]) -> set[str]:
        """Lines that could carry this reference, by any recovery mechanism.

        Exact and prefix hits are looked up globally, because a correct
        reference is decisive evidence and should not be missed just because
        the credit landed outside the expected window -- that case has to
        surface as a date exception, not vanish.

        Fuzzy comparison is deliberately restricted to the date window. Edit
        distance over the whole statement would both cost more and start
        producing coincidental hits.
        """
        target = normalize_ref(utr)
        if not target:
            return set()

        # Stage 1: hash lookups. A reference printed intact -- the overwhelming
        # majority -- is found here for the cost of two dict hits.
        found = set(self.by_token.get(target, ()))
        found |= self.by_prefix.get(target[:_PREFIX_KEY_LENGTH], set())

        # Stage 2: substring scan of the window, for references that survived
        # but were split across token boundaries by an unusual separator.
        for line_id in window - found:
            if target in self.facts[line_id].flattened:
                found.add(line_id)

        # Stage 3: edit distance, the only expensive step, and deliberately
        # last. It runs solely when the cheap stages found nothing at all --
        # once a reference has matched intact somewhere, a near-miss elsewhere
        # is noise, and paying for it on every settlement was the single
        # largest cost in the cascade.
        if found:
            return found

        for line_id in window:
            facts = self.facts[line_id]
            for token in facts.tokens:
                if len(token) < 10 or abs(len(token) - len(target)) > 2:
                    continue
                if bounded_edit_distance(token, target, 2) <= 2:
                    found.add(line_id)
                    break
        return found

    def amount_candidates(self, amount: int, window: set[str]) -> set[str]:
        """Lines in the window whose amount is exactly `amount`."""
        return self.by_amount.get(amount, set()) & window

    def near_amount_candidates(
        self, amount: int, window: set[str], tolerance: int
    ) -> set[str]:
        """Lines in the window within `tolerance` paise of `amount`."""
        found: set[str] = set()
        for delta in range(-tolerance, tolerance + 1):
            found |= self.by_amount.get(amount + delta, set()) & window
        return found


@dataclass(slots=True)
class SettlementIndex:
    """Settlements, indexed by the same access patterns as the bank side."""

    by_id: dict[str, Settlement] = field(default_factory=dict)
    by_date: dict[date, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_amount: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_utr: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    @classmethod
    def build(cls, settlements: Iterable[Settlement]) -> "SettlementIndex":
        index = cls()
        for settlement in settlements:
            index.by_id[settlement.settlement_id] = settlement
            index.by_date[settlement.settled_on].add(settlement.settlement_id)
            index.by_amount[settlement.amount].add(settlement.settlement_id)
            if settlement.utr:
                index.by_utr[normalize_ref(settlement.utr)].add(settlement.settlement_id)
        return index

    def in_window(self, start: date, end: date) -> set[str]:
        found: set[str] = set()
        current = start
        while current <= end:
            found |= self.by_date.get(current, set())
            current += timedelta(days=1)
        return found

    def amount_candidates(self, amount: int, window: set[str]) -> set[str]:
        return self.by_amount.get(amount, set()) & window

    def duplicate_utrs(self) -> dict[str, set[str]]:
        """References claimed by more than one settlement.

        A reference is supposed to be unique per payout. Two settlements
        sharing one means either the PSP reused it or the export was replayed,
        and both are conditions under which matching must not proceed silently.
        """
        return {utr: ids for utr, ids in self.by_utr.items() if len(ids) > 1}
