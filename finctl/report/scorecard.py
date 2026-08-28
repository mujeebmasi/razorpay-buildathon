"""Measuring the run against the held-out truth.

The metric that matters most here is not the match rate. It is the **false
match rate**: how often the engine confidently paired records that do not
belong together, or claimed to resolve a case that is unresolvable by
construction. A wrong match is worse than no match, because an operator has to
discover it before they can fix it, and nothing in the output flags it.

So every case gets one of eight verdicts, and they are deliberately not
collapsed into a single number. An engine that matches everything scores a
perfect match rate and a catastrophic false-match rate, and the scorecard has
to make that visible rather than average it away.

Reason-code accuracy is scored separately and more strictly than match/no-match.
Flagging the right record for the wrong reason sends it to the wrong person,
which is most of the cost of the break.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from finctl.engine.reconcile import ReconResult
from finctl.models import Match, Exception_


class Verdict(str, Enum):
    """The outcome of one case, from the scorer's point of view."""

    CORRECT_MATCH = "correct_match"
    WRONG_MATCH = "wrong_match"                  # matched, but to the wrong records
    MISSED_MATCH = "missed_match"                # resolvable, left unresolved
    CORRECT_EXCEPTION = "correct_exception"
    EXCEPTION_WRONG_REASON = "exception_wrong_reason"
    FALSE_MATCH = "false_match"                  # unresolvable, claimed as resolved
    MISSED_EXCEPTION = "missed_exception"        # break, reported nothing at all
    CORRECT_IGNORE = "correct_ignore"
    FALSE_BREAK = "false_break"                  # informational, reported as a break

    @property
    def is_good(self) -> bool:
        return self in {
            Verdict.CORRECT_MATCH,
            Verdict.CORRECT_EXCEPTION,
            Verdict.CORRECT_IGNORE,
        }

    @property
    def is_dangerous(self) -> bool:
        """Verdicts that put a wrong number in front of a human as if it were right."""
        return self in {Verdict.WRONG_MATCH, Verdict.FALSE_MATCH}


@dataclass(slots=True)
class CaseScore:
    """How one generated case was judged."""

    case_id: str
    scenario: str
    difficulty: str
    verdict: Verdict
    expected_reason: str
    actual_reason: str = ""
    detail: str = ""


@dataclass(slots=True)
class Scorecard:
    """Aggregate results, sliced the ways that reveal different failures."""

    cases: list[CaseScore] = field(default_factory=list)
    by_scenario: dict[str, dict[str, int]] = field(default_factory=dict)
    by_difficulty: dict[str, dict[str, int]] = field(default_factory=dict)

    # -- headline figures -------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.cases)

    def count(self, *verdicts: Verdict) -> int:
        wanted = set(verdicts)
        return sum(1 for case in self.cases if case.verdict in wanted)

    @property
    def resolvable(self) -> int:
        """Cases whose correct outcome is an automatic match."""
        return self.count(
            Verdict.CORRECT_MATCH, Verdict.WRONG_MATCH, Verdict.MISSED_MATCH
        )

    @property
    def breaks(self) -> int:
        """Cases whose correct outcome is an entry on the exception register."""
        return self.count(
            Verdict.CORRECT_EXCEPTION, Verdict.EXCEPTION_WRONG_REASON,
            Verdict.FALSE_MATCH, Verdict.MISSED_EXCEPTION,
        )

    @property
    def match_precision(self) -> float:
        """Of everything the engine matched, how much was actually right.

        The denominator includes false matches on unresolvable cases, which is
        the point: claiming to have solved something undecidable is a precision
        failure, not a separate category to be reported elsewhere.
        """
        claimed = self.count(
            Verdict.CORRECT_MATCH, Verdict.WRONG_MATCH, Verdict.FALSE_MATCH
        )
        return self.count(Verdict.CORRECT_MATCH) / claimed if claimed else 1.0

    @property
    def match_recall(self) -> float:
        """Of everything that should have matched, how much did."""
        return (
            self.count(Verdict.CORRECT_MATCH) / self.resolvable
            if self.resolvable else 1.0
        )

    @property
    def match_f1(self) -> float:
        precision, recall = self.match_precision, self.match_recall
        return (
            2 * precision * recall / (precision + recall)
            if precision + recall else 0.0
        )

    @property
    def exception_recall(self) -> float:
        """Of everything that should have been flagged, how much was."""
        caught = self.count(Verdict.CORRECT_EXCEPTION, Verdict.EXCEPTION_WRONG_REASON)
        return caught / self.breaks if self.breaks else 1.0

    @property
    def reason_accuracy(self) -> float:
        """Of the breaks caught, how many carry the correct reason code."""
        caught = self.count(Verdict.CORRECT_EXCEPTION, Verdict.EXCEPTION_WRONG_REASON)
        return self.count(Verdict.CORRECT_EXCEPTION) / caught if caught else 1.0

    @property
    def false_match_rate(self) -> float:
        """The number that should be looked at first. Lower is the whole game."""
        return (
            self.count(Verdict.WRONG_MATCH, Verdict.FALSE_MATCH) / self.total
            if self.total else 0.0
        )

    @property
    def auto_resolve_rate(self) -> float:
        """Share of all cases closed correctly with no human involvement."""
        return (
            self.count(Verdict.CORRECT_MATCH, Verdict.CORRECT_IGNORE) / self.total
            if self.total else 0.0
        )

    @property
    def accuracy(self) -> float:
        """Share of cases where the engine reached the correct conclusion."""
        return (
            sum(1 for case in self.cases if case.verdict.is_good) / self.total
            if self.total else 0.0
        )

    def failures(self, limit: int = 40) -> list[CaseScore]:
        """The cases that went wrong, worst first."""
        order = {
            Verdict.FALSE_MATCH: 0, Verdict.WRONG_MATCH: 1, Verdict.FALSE_BREAK: 2,
            Verdict.MISSED_EXCEPTION: 3, Verdict.MISSED_MATCH: 4,
            Verdict.EXCEPTION_WRONG_REASON: 5,
        }
        bad = [case for case in self.cases if not case.verdict.is_good]
        bad.sort(key=lambda c: (order.get(c.verdict, 9), c.scenario, c.case_id))
        return bad[:limit]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total,
            "accuracy": round(self.accuracy, 4),
            "auto_resolve_rate": round(self.auto_resolve_rate, 4),
            "match_precision": round(self.match_precision, 4),
            "match_recall": round(self.match_recall, 4),
            "match_f1": round(self.match_f1, 4),
            "exception_recall": round(self.exception_recall, 4),
            "reason_accuracy": round(self.reason_accuracy, 4),
            "false_match_rate": round(self.false_match_rate, 4),
            "verdicts": {
                verdict.value: self.count(verdict) for verdict in Verdict
            },
            "by_scenario": self.by_scenario,
            "by_difficulty": self.by_difficulty,
        }


class _Lookups:
    """Reverse indexes from record id to whatever the engine concluded about it."""

    def __init__(self, result: ReconResult) -> None:
        self.match_by_settlement: dict[str, Match] = {}
        self.match_by_line: dict[str, Match] = {}
        self.exceptions_by_subject: dict[str, list[Exception_]] = defaultdict(list)
        self.exception_candidates: dict[str, list[Exception_]] = defaultdict(list)

        for match in result.matches:
            for settlement_id in match.settlement_ids:
                self.match_by_settlement[settlement_id] = match
            for line_id in match.bank_line_ids:
                self.match_by_line[line_id] = match

        for exception in result.exceptions:
            for subject_id in exception.subject_ids:
                self.exceptions_by_subject[subject_id].append(exception)
            for candidate_id in exception.candidates:
                self.exception_candidates[candidate_id].append(exception)

    def any_exception_for(self, ids: Iterable[str]) -> list[Exception_]:
        found: list[Exception_] = []
        for record_id in ids:
            found.extend(self.exceptions_by_subject.get(record_id, ()))
            found.extend(self.exception_candidates.get(record_id, ()))
        return found


def _score_match_case(
    truth: Mapping[str, Any], lookups: _Lookups
) -> tuple[Verdict, str, str]:
    """Judge a case that should have resolved to a specific counterpart set."""
    expected_settlements = set(truth["settlement_ids"])
    expected_lines = set(truth["bank_line_ids"])

    match = None
    for settlement_id in sorted(expected_settlements):
        if found := lookups.match_by_settlement.get(settlement_id):
            match = found
            break
    if match is None:
        for line_id in sorted(expected_lines):
            if found := lookups.match_by_line.get(line_id):
                match = found
                break

    if match is None:
        raised = lookups.any_exception_for(expected_settlements | expected_lines)
        if raised:
            reason = raised[0].reason.value
            return (
                Verdict.MISSED_MATCH, reason,
                f"resolvable case raised as {reason} instead of matching",
            )
        return Verdict.MISSED_MATCH, "", "no match and no exception produced"

    got_settlements = set(match.settlement_ids)
    got_lines = set(match.bank_line_ids)

    # The engine must have paired exactly the records that belong together.
    # A superset is not a pass: sweeping in an extra settlement means some
    # other case has been robbed of its counterpart.
    if got_settlements == expected_settlements and got_lines == expected_lines:
        return Verdict.CORRECT_MATCH, match.reason.value, ""

    return (
        Verdict.WRONG_MATCH, match.reason.value,
        f"matched {sorted(got_settlements)} to {sorted(got_lines)}, expected "
        f"{sorted(expected_settlements)} to {sorted(expected_lines)}",
    )


def _score_exception_case(
    truth: Mapping[str, Any], lookups: _Lookups
) -> tuple[Verdict, str, str]:
    """Judge a case whose correct outcome is an entry on the exception register."""
    subjects = (
        set(truth["settlement_ids"]) | set(truth["bank_line_ids"])
        | set(truth["payment_ids"]) | set(truth.get("record_ids", ()))
    )
    expected_reason = truth["expected_reason"]

    raised = lookups.any_exception_for(subjects)
    if raised:
        reasons = {exception.reason.value for exception in raised}
        if expected_reason in reasons:
            return Verdict.CORRECT_EXCEPTION, expected_reason, ""
        actual = sorted(reasons)[0]
        return (
            Verdict.EXCEPTION_WRONG_REASON, actual,
            f"flagged as {sorted(reasons)}, expected {expected_reason}",
        )

    # Nothing was flagged. If the engine instead matched these records, it has
    # claimed to resolve something unresolvable, which is the dangerous case.
    for record_id in sorted(subjects):
        if record_id in lookups.match_by_settlement or record_id in lookups.match_by_line:
            match = (
                lookups.match_by_settlement.get(record_id)
                or lookups.match_by_line[record_id]
            )
            return (
                Verdict.FALSE_MATCH, match.reason.value,
                f"claimed a {match.reason.value} match on a case that cannot be "
                f"resolved from the data",
            )
    return Verdict.MISSED_EXCEPTION, "", "break neither matched nor flagged"


def _score_ignore_case(
    truth: Mapping[str, Any], lookups: _Lookups
) -> tuple[Verdict, str, str]:
    """Judge a case that must be absorbed silently rather than raised."""
    subjects = set(truth["bank_line_ids"]) | set(truth["settlement_ids"])
    raised = lookups.any_exception_for(subjects)
    if raised:
        reasons = sorted({exception.reason.value for exception in raised})
        return (
            Verdict.FALSE_BREAK, reasons[0],
            f"informational records raised as {reasons}",
        )
    return Verdict.CORRECT_IGNORE, "", ""


def score(result: ReconResult, truth_records: Sequence[Mapping[str, Any]]) -> Scorecard:
    """Compare a run against ground truth and produce the scorecard."""
    lookups = _Lookups(result)
    card = Scorecard()

    scenario_tally: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    difficulty_tally: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for truth in truth_records:
        disposition = truth["disposition"]
        if disposition == "match":
            verdict, actual, detail = _score_match_case(truth, lookups)
        elif disposition == "exception":
            verdict, actual, detail = _score_exception_case(truth, lookups)
        else:
            verdict, actual, detail = _score_ignore_case(truth, lookups)

        card.cases.append(
            CaseScore(
                case_id=truth["case_id"],
                scenario=truth["scenario"],
                difficulty=truth["difficulty"],
                verdict=verdict,
                expected_reason=truth["expected_reason"],
                actual_reason=actual,
                detail=detail,
            )
        )
        scenario_tally[truth["scenario"]][verdict.value] += 1
        difficulty_tally[truth["difficulty"]][verdict.value] += 1

    card.by_scenario = {k: dict(v) for k, v in sorted(scenario_tally.items())}
    card.by_difficulty = {k: dict(v) for k, v in sorted(difficulty_tally.items())}
    return card
