"""Record real agent investigations so they can be shown without an API key.

The agent is the part of this project a reviewer most needs to see, and it is
the part they are least likely to be able to run: it needs a key, a network,
and a provider that is not rate-limiting them at that moment.

So a real run is recorded once, against real data, with the full transcript of
every tool call and every result, and committed. The dashboard replays it and
labels it plainly as a recording with the model and timestamp attached. Nothing
is simulated and nothing is written by hand -- if the agent had reasoned
differently, the file would say something different.

    python -m finctl capture --data data --per-reason 2

Re-record after changing the tools or the prompt, or the shipped transcripts
will describe an agent that no longer exists.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from finctl.demo_agent import IN_ENGLISH, INTEREST, _candidates_for
from finctl.engine.reconcile import AdjudicationRequest
from finctl.models import Match, ReasonCode, Tier
from finctl.money import format_inr
from finctl.pipeline import run as run_pipeline
from finctl.verify.invariants import Verifier

#: Where the recording lives. Sits beside the data it describes, because a
#: transcript recorded against a different batch is worse than none.
TRANSCRIPT_FILE = "agent_transcripts.json"

#: Case kinds worth recording, hardest-judgement first. Deliberately spans both
#: the cases where refusing is correct and the ones where a signature has to be
#: named, so the recording is not a highlight reel of one behaviour.
WANTED = (
    "ambiguous_candidates",
    "amount_mismatch",
    "transposition_suspected",
    "scale_error_suspected",
    "date_out_of_window",
    "missing_bank_credit",
)


def capture(
    data_dir: Path,
    *,
    per_reason: int = 2,
    provider: str = "groq",
    model: str | None = None,
) -> int:
    """Run the agent over a spread of case types and write the transcripts."""
    from finctl.adjudicate.agent import AgentAdjudicator

    print(f"running the deterministic cascade over {data_dir} ...")
    baseline = run_pipeline(data_dir, adjudicator=None)
    settlements = baseline.batch.index_settlements()
    print(
        f"  {baseline.records:,} records -> {len(baseline.matches):,} matched, "
        f"{len(baseline.exceptions):,} unresolved"
    )

    # Group the unresolved cases by why the cascade gave up, so the recording
    # covers the range rather than whatever happens to be largest.
    by_reason: dict[str, list] = {}
    for exception in baseline.exceptions:
        if not exception.candidates:
            continue
        if not any(sid in settlements for sid in exception.subject_ids):
            continue
        by_reason.setdefault(exception.reason.value, []).append(exception)

    selected: list = []
    for reason in WANTED:
        group = sorted(by_reason.get(reason, []), key=lambda e: -e.amount)
        selected.extend(group[:per_reason])

    if not selected:
        print("nothing to record.")
        return 1

    budget = len(selected)
    try:
        agent = AgentAdjudicator(
            baseline.batch, provider=provider, model=model, case_budget=budget
        )
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1

    print(f"\nagent: {agent.name}")
    print(f"recording {budget} case(s) across {len(WANTED)} kinds ...\n")

    records: list[dict[str, Any]] = []
    for index, exception in enumerate(selected, start=1):
        settlement_id = next(s for s in exception.subject_ids if s in settlements)
        settlement = settlements[settlement_id]
        candidates = _candidates_for(exception, baseline.batch)
        if not candidates:
            continue

        outcome = agent.adjudicate(
            AdjudicationRequest(
                subject_kind="settlement",
                subject_id=settlement_id,
                subject_amount=settlement.amount,
                subject_date=settlement.settled_on,
                subject_description="",
                candidates=tuple(candidates),
            )
        )

        steps = agent.transcripts.get(settlement_id, [])

        # Put the proposal through the verifier exactly as the pipeline would.
        # A recording that stopped at the agent's conclusion would show only
        # half the mechanism -- and the half that matters most is what happens
        # to a confident proposal that does not balance.
        verdict: dict[str, Any] = {"ran": False}
        if outcome.decision == "match" and outcome.chosen_ids:
            lines = baseline.batch.index_bank_lines()
            chosen = outcome.chosen_ids[0]
            proposal = Match.create(
                bank_line_ids=[chosen],
                settlement_ids=[settlement_id],
                tier=Tier.T7_ADJUDICATED,
                reason=ReasonCode.ADJUDICATED_MATCH,
                confidence=outcome.confidence,
                bank_total=lines[chosen].amount,
                expected_total=settlement.amount,
                evidence=outcome.evidence,
                rationale=outcome.rationale,
                adjudicator=agent.name,
            )
            report = Verifier(baseline.batch).verify([proposal])
            accepted = bool(report.accepted)
            verdict = {
                "ran": True,
                "accepted": accepted,
                "violations": [
                    {"invariant": v.invariant.value, "detail": v.detail}
                    for v in report.violations
                ],
            }
        records.append({
            "settlement_id": settlement_id,
            "reason": exception.reason.value,
            "reason_in_english": IN_ENGLISH.get(exception.reason.value, ""),
            "payout_amount": format_inr(settlement.amount),
            "payout_date": settlement.settled_on.isoformat(),
            "reference": settlement.utr,
            "candidate_count": len(candidates),
            "candidates": [
                {"line_id": c["id"], "amount": format_inr(int(c["amount"])),
                 "date": c["date"]}
                for c in candidates[:6]
            ],
            "steps": [
                {
                    "tool": step["tool"],
                    "arguments": step["arguments"],
                    # The raw tool result. Kept whole where it reasonably can
                    # be: a result trimmed mid-JSON is no longer parseable, and
                    # the dashboard renders it verbatim.
                    "result": step["result"][:2000],
                }
                for step in steps
            ],
            "decision": "match" if outcome.decision == "match" else "decline",
            "chosen": outcome.chosen_ids[0] if outcome.chosen_ids else None,
            "confidence": round(outcome.confidence, 3),
            "reasoning": outcome.rationale,
            "verifier": verdict,
        })

        mark = "match" if outcome.decision == "match" else "decline"
        if verdict.get("ran") and not verdict.get("accepted"):
            mark = "match -> VETOED by verifier"
        print(f"  {index:>2}/{budget}  {exception.reason.value:<24} "
              f"{len(steps)} tool call(s) -> {mark}")

    payload = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": provider,
        "model": agent.model,
        "data_dir": str(data_dir),
        "usage": agent.usage.as_dict(),
        "cases": records,
    }

    target = data_dir / TRANSCRIPT_FILE
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    usage = agent.usage.as_dict()
    print(f"\nwrote {len(records)} transcript(s) to {target}")
    print(f"  {usage['decided']} matched, {usage['declined']} declined, "
          f"{usage['failed']} failed, {usage['tool_calls']} tool calls "
          f"in {usage['seconds']:.1f}s")
    return 0


def load_transcripts(data_dir: Path) -> dict[str, Any] | None:
    """Read a recording, if one was committed alongside the data."""
    path = data_dir / TRANSCRIPT_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
