"""Watch the agent investigate, one case at a time.

`recon --adjudicator agent` runs the whole pipeline and reports totals, which
is the right shape for a batch job and the wrong shape for understanding what
the agent actually does. This runs the deterministic cascade first -- fast, and
exactly as it always runs -- then hands the genuinely unresolved cases to the
agent one at a time and prints each tool call as it happens.

    python -m finctl agent --data data --cases 3

It is the same `AgentAdjudicator` the pipeline uses, on the same real batch. The
only difference is that the reasoning is printed instead of aggregated.
"""
from __future__ import annotations

import sys
from pathlib import Path

from finctl.engine.reconcile import AdjudicationRequest
from finctl.ingest.loader import Batch
from finctl.models import Exception_
from finctl.money import format_inr
from finctl.pipeline import run as run_pipeline

_RULE = "-" * 78

#: Which unresolved cases are worth watching an agent work on, best first.
#:
#: "The money never arrived" is the most common outcome and the least
#: interesting: there is nothing to weigh, so the agent declines every time. A
#: contested credit or a suspected keying error is where two readings of the
#: evidence are both defensible, which is the only place judgement shows.
INTEREST: dict[str, int] = {
    "ambiguous_candidates": 0,     # two payouts fit one credit - a coin flip
    "amount_mismatch": 1,          # right reference, wrong amount
    "transposition_suspected": 2,  # digits swapped by a human
    "scale_error_suspected": 3,    # out by exactly 100x
    "date_out_of_window": 4,       # right money, arrived far too late
    "no_candidate": 5,
    "missing_bank_credit": 6,      # nothing to weigh; always declined
}

#: Plain-English gloss for the reason the cascade gave up, so the demo reads
#: to someone who does not work in settlements.
IN_ENGLISH: dict[str, str] = {
    "ambiguous_candidates":
        "two different payouts fit the same credit, and nothing separates them",
    "amount_mismatch":
        "the reference matches but the amount is short",
    "transposition_suspected":
        "the amounts use the same digits in a different order",
    "scale_error_suspected":
        "the two amounts are out by exactly 100x",
    "date_out_of_window":
        "the money matches but arrived far outside the agreed window",
    "missing_bank_credit":
        "no matching credit was found in the bank statement at all",
    "no_candidate":
        "nothing in the window resembles this payout",
}


def _candidates_for(exception: Exception_, batch: Batch) -> list[dict]:
    """The credits the cascade considered and could not choose between."""
    lines = batch.index_bank_lines()
    return [
        {
            "id": line_id,
            "amount": lines[line_id].amount,
            "date": lines[line_id].value_date.isoformat(),
        }
        for line_id in exception.candidates
        if line_id in lines and lines[line_id].is_credit
    ]


def demo(
    data_dir: Path,
    *,
    cases: int = 3,
    provider: str = "groq",
    model: str | None = None,
    reason: str | None = None,
) -> int:
    """Run the cascade, then narrate the agent on what it left behind."""
    from finctl.adjudicate.agent import AgentAdjudicator

    print(f"\nrunning the deterministic cascade over {data_dir} ...")
    baseline = run_pipeline(data_dir, adjudicator=None)
    settlements = baseline.batch.index_settlements()

    print(
        f"  {baseline.records:,} records in {baseline.total_seconds:.2f}s  "
        f"-> {len(baseline.matches):,} matched, "
        f"{len(baseline.exceptions):,} left unresolved"
    )

    # Only cases with real candidates are worth an agent: an exception with
    # nothing to choose between is a break, not a judgement call.
    open_cases = [
        exception for exception in baseline.exceptions
        if exception.candidates
        and any(sid in settlements for sid in exception.subject_ids)
    ]
    if not open_cases:
        print("\nnothing left ambiguous enough to need an agent.")
        return 0

    if reason:
        open_cases = [e for e in open_cases if e.reason.value == reason]
        if not open_cases:
            print(f"\nno unresolved cases with reason {reason!r}.")
            print(f"available: {', '.join(sorted(INTEREST))}")
            return 1

    # Rank by how much judgement the case actually needs, not by value.
    #
    # Sorting by amount surfaces "the money never arrived" every time, because
    # those are the largest -- and the answer there is always the same. The
    # cases worth watching an agent on are the ones where two readings of the
    # evidence are both defensible. Value breaks ties within a band.
    open_cases.sort(key=lambda e: (INTEREST.get(e.reason.value, 99), -e.amount))
    selected = open_cases[:cases]

    try:
        agent = AgentAdjudicator(
            baseline.batch, provider=provider, model=model, case_budget=cases
        )
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    print(f"\nagent: {agent.name}")
    print(f"handing it the {len(selected)} case(s) needing the most judgement.\n")

    for index, exception in enumerate(selected, start=1):
        settlement_id = next(
            sid for sid in exception.subject_ids if sid in settlements
        )
        settlement = settlements[settlement_id]
        candidates = _candidates_for(exception, baseline.batch)
        if not candidates:
            continue

        print(_RULE)
        print(f"CASE {index}/{len(selected)}   {settlement_id}")
        print(f"  payout    {format_inr(settlement.amount)}  {settlement.settled_on}")
        print(f"  reference {settlement.utr or 'none on file'}")
        gloss = IN_ENGLISH.get(exception.reason.value, "")
        print(f"  cascade   gave up: {exception.reason.value}")
        if gloss:
            print(f"            ({gloss})")
        print(f"  offered   {len(candidates)} candidate credit(s)")
        print()

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

        trace = agent.traces.get(settlement_id, [])
        if trace:
            print("  the agent chose to look at:")
            for step, call in enumerate(trace, start=1):
                print(f"    {step}. {call}")
        else:
            print("  (no tools called)")

        verdict = "MATCHED" if outcome.decision == "match" else "DECLINED"
        target = f" -> {outcome.chosen_ids[0]}" if outcome.chosen_ids else ""
        print(f"\n  {verdict}{target}   confidence {outcome.confidence:.2f}")
        print(f"  reasoning: {outcome.rationale[:300]}")
        print()

    usage = agent.usage.as_dict()
    print(_RULE)
    print("AGENT TOTALS")
    print(f"  {usage['decided']} matched, {usage['declined']} declined, "
          f"{usage['failed']} failed")
    print(f"  {usage['tool_calls']} tool calls over {usage['requests']} requests "
          f"in {usage['seconds']:.1f}s")
    if usage["throttled"]:
        print(f"  throttled {usage['throttled']} time(s) by the provider's rate limit")
    print(f"  {usage['prompt_tokens']:,} tokens in / "
          f"{usage['completion_tokens']:,} out")
    print("\n  Every match above would still face the verifier, which recomputes")
    print("  from the source records and can veto it.\n")
    return 0
