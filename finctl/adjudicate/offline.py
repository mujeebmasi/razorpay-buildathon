"""The local adjudicator: a transparent weighted-evidence reasoner.

This is what decides the residual when no hosted model is configured. It is not
a language model and does not pretend to be one -- it is an explicit scoring
model whose weights are visible in the source, which for this particular job is
arguably the better tool: every decision is reproducible, costs nothing, and can
be explained to an auditor line by line.

Two design choices carry most of the value.

**A margin requirement, not just a threshold.** A candidate must not only score
well in absolute terms, it must beat the runner-up by a clear margin. Two
candidates scoring 0.81 and 0.79 mean the evidence does not distinguish them,
however high both numbers look. Without this rule a scoring model will always
return its argmax, and an argmax is not a decision -- it is a ranking with the
uncertainty thrown away.

**Abstention is a normal outcome.** Returning "I cannot tell" is what a careful
analyst does with genuinely ambiguous evidence, and the cascade treats it as a
valid result rather than a failure. An adjudicator that never abstains is not
more capable, it is less honest.

Everything this produces is still subject to the verifier. Nothing here can
create a match that does not balance.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from finctl.engine.reconcile import (
    AdjudicationRequest, AdjudicationResult,
)
from finctl.models import Evidence
from finctl.money import format_inr

# Weights for each signal. Positive supports a pairing, negative counts against
# it. These are ordered by how much a settlements analyst would actually trust
# them: a recovered reference outweighs everything, an exact amount is strong
# corroboration, and lateness is a mild penalty rather than a disqualifier.
WEIGHTS: Final[dict[str, float]] = {
    "reference_strong": 0.55,      # score >= 0.85: intact or one edit away
    "reference_partial": 0.28,     # score >= 0.60: a truncated prefix
    "amount_exact": 0.35,
    "amount_within_tolerance": 0.22,
    "same_day": 0.12,
    "within_window": 0.06,
    "late_penalty": -0.05,         # per banking day beyond the window
    "amount_far_penalty": -0.45,
    "scale_error_penalty": -0.60,  # a unit bug, not a match
    "transposition_penalty": -0.50,
    "fee_inversion_support": 0.15,
}

#: A candidate must reach this to be considered at all.
DECISION_THRESHOLD: Final[float] = 0.60

#: ...and must beat the runner-up by this much. This is the abstention rule.
MARGIN_REQUIRED: Final[float] = 0.15


@dataclass(frozen=True, slots=True)
class _Scored:
    """One candidate with its score decomposed into named contributions."""

    candidate_id: str
    score: float
    contributions: tuple[tuple[str, float, str], ...]

    def explain(self) -> str:
        parts = [
            f"{label} ({weight:+.2f})"
            for label, weight, _ in self.contributions
            if abs(weight) >= 0.01
        ]
        return ", ".join(parts)


class OfflineAdjudicator:
    """Scores each candidate on measured evidence, then decides or abstains."""

    name = "local-evidence-reasoner/1.0"

    #: Reported alongside every decision so a reader knows what produced it.
    #: The cascade surfaces this in the dashboard rather than implying that a
    #: language model was involved when none was.
    kind = "deterministic local reasoning engine"

    def __init__(
        self,
        *,
        threshold: float = DECISION_THRESHOLD,
        margin: float = MARGIN_REQUIRED,
        tolerance: int = 5,
    ) -> None:
        self.threshold = threshold
        self.margin = margin
        self.tolerance = tolerance

    def _score(self, candidate: dict, subject_amount: int) -> _Scored:
        contributions: list[tuple[str, float, str]] = []

        reference_score = float(candidate.get("reference_score", 0.0))
        mechanism = candidate.get("reference_mechanism", "no_match")
        if reference_score >= 0.85:
            contributions.append((
                "reference recovered",
                WEIGHTS["reference_strong"] * reference_score,
                f"{mechanism} at {reference_score:.2f}",
            ))
        elif reference_score >= 0.60:
            contributions.append((
                "partial reference",
                WEIGHTS["reference_partial"] * reference_score,
                f"{mechanism} at {reference_score:.2f}",
            ))

        delta = int(candidate.get("delta", 0))
        if delta == 0:
            contributions.append((
                "amount exact", WEIGHTS["amount_exact"],
                f"credit equals the payout to the paise",
            ))
        elif abs(delta) <= self.tolerance:
            contributions.append((
                "amount within tolerance", WEIGHTS["amount_within_tolerance"],
                f"{delta:+d} paise",
            ))
        elif candidate.get("scale_error"):
            contributions.append((
                "unit error", WEIGHTS["scale_error_penalty"],
                "the two figures differ by a factor of 100",
            ))
        elif candidate.get("transposition"):
            contributions.append((
                "digits transposed", WEIGHTS["transposition_penalty"],
                "same digits in a different order",
            ))
        else:
            contributions.append((
                "amount differs", WEIGHTS["amount_far_penalty"],
                f"{format_inr(abs(delta))} apart",
            ))

        late = int(candidate.get("banking_days_late", 0))
        if late == 0:
            contributions.append(("same banking day", WEIGHTS["same_day"], ""))
        elif 0 < late <= 3:
            contributions.append((
                "inside the window", WEIGHTS["within_window"],
                f"{late} banking day(s) after the payout",
            ))
        else:
            contributions.append((
                "outside the window", WEIGHTS["late_penalty"] * abs(late),
                f"{late} banking days from the payout",
            ))

        # A gross amount recoverable from the credit by inverting the rate card
        # is independent corroboration: it means the credit is consistent with
        # being the net of a real payment rather than an arbitrary figure.
        if candidate.get("fee_inversions"):
            methods = ", ".join(list(candidate["fee_inversions"])[:2])
            contributions.append((
                "consistent with the rate card", WEIGHTS["fee_inversion_support"],
                f"invertible as a {methods} net",
            ))

        score = sum(weight for _, weight, _ in contributions)
        return _Scored(
            candidate_id=candidate["id"],
            score=round(max(0.0, min(1.0, score)), 4),
            contributions=tuple(contributions),
        )

    def adjudicate(self, request: AdjudicationRequest) -> AdjudicationResult:
        """Decide, or decline, with the reasoning attached either way."""
        if not request.candidates:
            return AdjudicationResult(
                decision="abstain", chosen_ids=(), confidence=0.0,
                rationale="no candidate credits fell within the settlement window",
            )

        scored = sorted(
            (self._score(c, request.subject_amount) for c in request.candidates),
            key=lambda s: (-s.score, s.candidate_id),
        )
        best = scored[0]
        runner_up = scored[1] if len(scored) > 1 else None
        margin = best.score - (runner_up.score if runner_up else 0.0)

        evidence = tuple(
            Evidence(
                kind="adjudicator_signal",
                detail=f"{label}: {note}" if note else label,
                weight=weight,
                record_ids=(request.subject_id, best.candidate_id),
            )
            for label, weight, note in best.contributions
        )

        if best.score < self.threshold:
            return AdjudicationResult(
                decision="abstain", chosen_ids=(), confidence=best.score,
                rationale=(
                    f"the strongest candidate scored {best.score:.2f}, below the "
                    f"{self.threshold:.2f} required to decide. Evidence: "
                    f"{best.explain()}"
                ),
                evidence=evidence,
            )

        if runner_up is not None and margin < self.margin:
            return AdjudicationResult(
                decision="abstain", chosen_ids=(), confidence=best.score,
                rationale=(
                    f"two candidates are too close to separate: "
                    f"{best.candidate_id} at {best.score:.2f} against "
                    f"{runner_up.candidate_id} at {runner_up.score:.2f}, a margin of "
                    f"{margin:.2f} where {self.margin:.2f} is required. The evidence "
                    f"does not distinguish them, so this needs a human."
                ),
                evidence=evidence + (
                    Evidence(
                        "runner_up",
                        f"{runner_up.candidate_id} scored {runner_up.score:.2f} on "
                        f"{runner_up.explain()}",
                        0.0, (request.subject_id, runner_up.candidate_id),
                    ),
                ),
            )

        return AdjudicationResult(
            decision="match",
            chosen_ids=(best.candidate_id,),
            confidence=best.score,
            rationale=(
                f"{best.candidate_id} is the only candidate the evidence supports, "
                f"scoring {best.score:.2f}"
                + (
                    f" against {runner_up.score:.2f} for the next best, a margin of "
                    f"{margin:.2f}."
                    if runner_up else " with no competing candidate."
                )
                + f" Basis: {best.explain()}."
            ),
            evidence=evidence,
        )
