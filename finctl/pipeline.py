"""The end-to-end run: ingest, reconcile, verify, post, score.

One function, one result object, so the CLI, the dashboard and the tests all
exercise exactly the same path. Anything that only the demo does is a thing
that has never really been tested.

The stage order encodes the trust model:

    ingest      -> parse or quarantine; nothing is coerced
    reconcile   -> deterministic cascade, then the adjudicator on the residual
    verify      -> independent arithmetic check with the power to veto
    post        -> only verified matches reach the journal
    score       -> compare against held-out truth, if labels are available

Verification sits between reasoning and posting on purpose. Nothing a
reasoning component proposes can reach the books without balancing first.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from finctl.engine.reconcile import Adjudicator, ReconConfig, Reconciler, ReconResult
from finctl.ingest.loader import Batch, load_batch, load_truth
from finctl.models import Exception_, Match, Severity
from finctl.post.journal import PostingResult, post_matches
from finctl.report.scorecard import Scorecard, score
from finctl.verify.invariants import (
    VerificationReport, Verifier, rejections_to_exceptions,
)


@dataclass(slots=True)
class RunResult:
    """Everything one end-to-end run produced."""

    batch: Batch
    recon: ReconResult
    verification: VerificationReport
    posting: PostingResult
    scorecard: Scorecard | None
    matches: list[Match] = field(default_factory=list)
    exceptions: list[Exception_] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    total_seconds: float = 0.0
    #: Populated only when a tool-using agent ran.
    agent_usage: dict[str, Any] | None = None
    agent_traces: dict[str, list[str]] = field(default_factory=dict)

    # -- headline operational figures -------------------------------------

    @property
    def records(self) -> int:
        return self.batch.record_count

    @property
    def throughput(self) -> float:
        return self.records / self.total_seconds if self.total_seconds else 0.0

    @property
    def value_matched(self) -> int:
        return sum(match.bank_total for match in self.matches)

    @property
    def value_at_risk(self) -> int:
        """Money sitting in unresolved breaks. The number a CFO asks for first."""
        return sum(
            exception.amount for exception in self.exceptions
            if exception.severity in {Severity.CRITICAL, Severity.HIGH}
        )

    @property
    def match_rate(self) -> float:
        """Share of settlements closed by a verified match.

        Deliberately computed over settlements rather than over all records:
        quoting a rate against a denominator padded with payments and ledger
        rows would inflate it for free.
        """
        total = len(self.batch.settlements)
        if not total:
            return 0.0
        settled = {sid for match in self.matches for sid in match.settlement_ids}
        return len(settled) / total

    @property
    def exceptions_by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for exception in self.exceptions:
            counts[exception.severity.value] = counts.get(exception.severity.value, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        """Flat summary for the dashboard and for machine consumption."""
        data: dict[str, Any] = {
            "records": self.records,
            "seconds": round(self.total_seconds, 3),
            "throughput_per_second": round(self.throughput, 1),
            "settlements": len(self.batch.settlements),
            "bank_lines": len(self.batch.bank_lines),
            "payments": len(self.batch.payments),
            "quarantined": len(self.batch.quarantined),
            "matches": len(self.matches),
            "exceptions": len(self.exceptions),
            "match_rate": round(self.match_rate, 4),
            "value_matched": self.value_matched,
            "value_at_risk": self.value_at_risk,
            "tier_counts": self.recon.tier_counts,
            "reason_counts": self.recon.reason_counts,
            "severity_counts": self.exceptions_by_severity,
            "stage_timings": {k: round(v, 4) for k, v in self.timings.items()},
            "adjudicator": self.recon.adjudicator_name,
            "adjudicated": self.recon.counters.get("adjudicated", 0),
            "adjudicator_abstained": self.recon.counters.get("adjudicator_abstained", 0),
            "verifier_checks": self.verification.checks_run,
            "verifier_rejections": self.verification.rejection_count,
            "verifier_rejected_adjudications": self.verification.adjudicated_rejections,
            "journal_entries": len(self.posting.entries),
            "journal_balances": self.posting.balances,
            "journal_debits": self.posting.total_debits,
            "journal_credits": self.posting.total_credits,
            "counters": dict(self.recon.counters),
        }
        if self.scorecard is not None:
            data["scorecard"] = self.scorecard.as_dict()
        if self.agent_usage is not None:
            data["agent"] = self.agent_usage
        return data


def run(
    data_dir: Path,
    *,
    config: ReconConfig | None = None,
    adjudicator: Adjudicator | None = None,
    adjudicator_factory: Callable[[Batch], Adjudicator] | None = None,
    with_truth: bool = True,
) -> RunResult:
    """Execute the full loop over one data directory.

    An adjudicator may be supplied directly, or as a factory taking the loaded
    batch. The tool-using agent needs the batch to build its investigation
    toolbox, and it cannot exist before ingest has run.
    """
    started = time.perf_counter()
    timings: dict[str, float] = {}

    stage = time.perf_counter()
    batch = load_batch(data_dir)
    timings["ingest"] = time.perf_counter() - stage

    if adjudicator_factory is not None:
        adjudicator = adjudicator_factory(batch)

    stage = time.perf_counter()
    recon = Reconciler(batch, config, adjudicator).run()
    timings["reconcile"] = time.perf_counter() - stage

    stage = time.perf_counter()
    verifier = Verifier(
        batch,
        amount_tolerance=(config or ReconConfig()).amount_tolerance,
    )
    verification = verifier.verify(recon.matches)
    timings["verify"] = time.perf_counter() - stage

    # Only verified matches survive. A rejected proposal becomes a break with
    # its reasoning attached, so the guardrail firing is visible rather than
    # silent.
    matches = list(verification.accepted)
    exceptions = list(recon.exceptions) + rejections_to_exceptions(verification)

    stage = time.perf_counter()
    posting = post_matches(matches, batch)
    timings["post"] = time.perf_counter() - stage

    scorecard = None
    if with_truth and (truth := load_truth(data_dir)):
        stage = time.perf_counter()
        # Scored against the post-verification picture, which is what a user
        # would actually act on -- not the pre-verification proposals.
        scored_recon = ReconResult(
            matches=matches,
            exceptions=exceptions,
            adjudicator_name=recon.adjudicator_name,
        )
        scorecard = score(scored_recon, truth)
        timings["score"] = time.perf_counter() - stage

    # Worst first, then by value, so the register opens on what matters.
    exceptions.sort(key=lambda e: (e.severity.rank, -e.amount, e.exception_id))

    return RunResult(
        agent_usage=getattr(adjudicator, "usage", None).as_dict()
        if hasattr(adjudicator, "usage") else None,
        agent_traces=dict(getattr(adjudicator, "traces", {}) or {}),
        batch=batch,
        recon=recon,
        verification=verification,
        posting=posting,
        scorecard=scorecard,
        matches=matches,
        exceptions=exceptions,
        timings=timings,
        total_seconds=time.perf_counter() - started,
    )
