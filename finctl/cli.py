"""Command line interface.

    python -m finctl recon --data data
    python -m finctl recon --data data --adjudicator claude
    python -m finctl exceptions --data data --severity high
    python -m finctl journal --data data
    python -m finctl serve --port 8000

Output is written for a terminal an operator is actually reading: the headline
figures first, then where the work went, then what is still open. The exception
register is the part that matters, so it is the part with the most detail.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from finctl.config import load_dotenv
from finctl.engine.reconcile import ReconConfig
from finctl.models import Severity
from finctl.money import format_inr
from finctl.pipeline import RunResult, run
from finctl.post.journal import trial_balance_report

# Plain ASCII rules so the output survives being pasted into a ticket.
_RULE = "-" * 78


def _configure_console() -> None:
    """Make stdout able to carry the rupee sign.

    The Windows console defaults to cp1252, which has no code point for the
    rupee sign, so printing a formatted amount raises UnicodeEncodeError and
    takes the whole run down after the work is already done. Switching the
    stream to UTF-8 fixes it where the terminal supports it; the
    backslashreplace fallback keeps output flowing where it does not, since a
    mangled currency symbol is a far better outcome than a crashed report.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


def _local():
    from finctl.adjudicate.offline import OfflineAdjudicator

    return OfflineAdjudicator()


def _build_adjudicator(kind: str):
    """Construct an adjudicator that needs no batch. Returns None for 'none'."""
    if kind == "none":
        return None
    if kind == "local":
        return _local()
    if kind == "claude":
        from finctl.adjudicate.claude import ClaudeAdjudicator

        try:
            return ClaudeAdjudicator()
        except RuntimeError as exc:
            print(f"warning: {exc}", file=sys.stderr)
            print("warning: falling back to the local reasoner.", file=sys.stderr)
            return _local()
    raise SystemExit(f"unknown adjudicator {kind!r}")


def _build_agent_factory(provider: str, model: str | None, budget: int):
    """A factory, because the agent's toolbox needs the loaded batch.

    Falls back to the local reasoner rather than aborting: a missing key or an
    unreachable host should degrade the run, not end it.
    """
    def factory(batch):
        from finctl.adjudicate.agent import AgentAdjudicator

        try:
            agent = AgentAdjudicator(
                batch, provider=provider, model=model, case_budget=budget
            )
        except RuntimeError as exc:
            print(f"warning: {exc}", file=sys.stderr)
            print("warning: falling back to the local reasoner.", file=sys.stderr)
            return _local()
        print(f"  agent: {agent.name} (budget {budget} cases)", file=sys.stderr)
        return agent

    return factory


def _header(title: str) -> None:
    print(f"\n{title}\n{_RULE}")


def _print_summary(result: RunResult) -> None:
    summary = result.summary()

    _header("THROUGHPUT")
    print(
        f"  {summary['records']:,} records reconciled in "
        f"{summary['seconds']:.2f}s  "
        f"({summary['throughput_per_second']:,.0f} records/second)"
    )
    print(
        f"  {summary['settlements']:,} payouts, {summary['bank_lines']:,} bank lines, "
        f"{summary['payments']:,} payments"
    )
    for stage, seconds in result.timings.items():
        print(f"    {stage:<12} {seconds * 1000:>8.1f} ms")

    _header("MATCHING")
    print(
        f"  match rate      {summary['match_rate'] * 100:.1f}%  "
        f"({summary['matches']:,} matches covering "
        f"{format_inr(summary['value_matched'])})"
    )
    tier_labels = {
        "T0": "exact reference", "T1": "reference recovered from narration",
        "T2": "unique amount and date", "T3": "within rounding tolerance",
        "T4": "fee model inverted", "T5": "batch decomposition",
        "T6": "global assignment", "T7": "adjudicated",
    }
    for tier, count in sorted(summary["tier_counts"].items()):
        share = count / max(summary["matches"], 1) * 100
        bar = "#" * max(1, round(share / 2.5))
        print(
            f"    {tier} {tier_labels.get(tier, ''):<36} {count:>5} "
            f"{share:>5.1f}%  {bar}"
        )

    _header("VERIFICATION")
    print(f"  {summary['verifier_checks']:,} matches independently re-checked")
    print(f"  {summary['verifier_rejections']:,} rejected for failing an invariant")
    if summary["verifier_rejected_adjudications"]:
        print(
            f"  {summary['verifier_rejected_adjudications']:,} of those were "
            f"adjudicator proposals overruled by arithmetic"
        )
    print(
        f"  journal: {summary['journal_entries']:,} entries, "
        f"debits {format_inr(summary['journal_debits'])} vs credits "
        f"{format_inr(summary['journal_credits'])} -- "
        f"{'BALANCED' if summary['journal_balances'] else 'OUT OF BALANCE'}"
    )

    if agent := summary.get("agent"):
        _header("AGENT")
        print(f"  {summary['adjudicator']}")
        print(f"  investigated   {agent['decided'] + agent['declined']:>5} cases "
              f"in {agent['seconds']:.1f}s")
        print(f"  tool calls     {agent['tool_calls']:>5}")
        print(f"  matched        {agent['decided']:>5}")
        print(f"  declined       {agent['declined']:>5}   <- refusing is a valid answer")
        if agent["failed"]:
            print(f"  failed         {agent['failed']:>5}   (degraded to abstention)")
        print(f"  tokens         {agent['prompt_tokens']:,} in / "
              f"{agent['completion_tokens']:,} out over {agent['requests']} requests")

    _header("EXCEPTIONS")
    print(
        f"  {summary['exceptions']:,} open items carrying "
        f"{format_inr(summary['value_at_risk'])} of high-severity exposure"
    )
    for severity in Severity:
        count = summary["severity_counts"].get(severity.value, 0)
        if count:
            print(f"    {severity.value:<10} {count:>5}")
    print()
    for reason, count in sorted(
        summary["reason_counts"].items(), key=lambda item: -item[1]
    ):
        print(f"    {reason:<28} {count:>5}")

    if result.scorecard is not None:
        card = result.scorecard
        _header("ACCURACY (measured against held-out labels)")
        print(f"  cases scored           {card.total:,}")
        print(f"  overall accuracy       {card.accuracy * 100:.1f}%")
        print(f"  auto-resolve rate      {card.auto_resolve_rate * 100:.1f}%")
        print(
            f"  match precision        {card.match_precision * 100:.1f}%   "
            f"recall {card.match_recall * 100:.1f}%   "
            f"F1 {card.match_f1 * 100:.1f}%"
        )
        print(f"  exception recall       {card.exception_recall * 100:.1f}%")
        print(f"  reason-code accuracy   {card.reason_accuracy * 100:.1f}%")
        print(
            f"  FALSE MATCH RATE       {card.false_match_rate * 100:.2f}%   "
            f"<- wrong answers presented as right"
        )
        failures = card.failures(8)
        if failures:
            print("\n  cases the engine did not get right:")
            for case in failures:
                print(
                    f"    {case.scenario:<24} {case.verdict.value:<24} "
                    f"{case.detail[:60]}"
                )


def _print_exceptions(result: RunResult, minimum: Severity, limit: int) -> None:
    ranked = [
        exception for exception in result.exceptions
        if exception.severity.rank <= minimum.rank
    ]
    _header(f"EXCEPTION REGISTER  ({len(ranked)} at {minimum.value} or above)")
    for exception in ranked[:limit]:
        print(
            f"\n[{exception.severity.value.upper():<8}] {exception.reason.value}  "
            f"{format_inr(exception.amount)}  {exception.as_of}"
        )
        print(f"  subject : {', '.join(exception.subject_ids[:3])}")
        print(f"  summary : {exception.summary}")
        print(f"  action  : {exception.owner} -- {exception.suggested_action}")
        for item in exception.evidence[:3]:
            print(f"  evidence: {item}")
        if exception.candidates:
            print(f"  looked at: {', '.join(exception.candidates[:4])}")
    if len(ranked) > limit:
        print(f"\n  ... and {len(ranked) - limit:,} more")


def _print_journal(result: RunResult, limit: int) -> None:
    posting = result.posting
    _header("TRIAL BALANCE")
    for account, amount, direction in trial_balance_report(posting):
        print(f"  {account:<34} {format_inr(amount):>18} {direction}")
    print(f"\n  {'total debits':<34} {format_inr(posting.total_debits):>18}")
    print(f"  {'total credits':<34} {format_inr(posting.total_credits):>18}")
    print(f"  {'balanced':<34} {str(posting.balances).upper():>18}")

    _header(f"JOURNAL ENTRIES  (showing {min(limit, len(posting.entries))} "
            f"of {len(posting.entries):,})")
    for entry in posting.entries[:limit]:
        print(f"\n  {entry.entry_id}  {entry.posted_on}")
        print(f"  {entry.narrative}")
        for line in entry.lines:
            side = "Dr" if line.debit else "  Cr"
            print(f"    {side} {line.account:<34} {format_inr(line.amount):>16}")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="finctl",
        description="Autonomous three-way settlement reconciliation.",
    )
    parser.add_argument(
        "command",
        choices=["recon", "exceptions", "journal", "serve"],
        help="recon: full run with scorecard. exceptions: the open register. "
             "journal: trial balance and entries. serve: the dashboard.",
    )
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument(
        "--adjudicator", choices=["none", "local", "claude", "agent"], default="local",
        help="who decides the residual: none, the offline reasoner, a single-shot "
             "Claude call, or the tool-using agent",
    )
    parser.add_argument(
        "--provider", choices=["groq", "gemini", "openai"], default="groq",
        help="LLM host for --adjudicator agent (OpenAI-compatible schema)",
    )
    parser.add_argument(
        "--model", default=None,
        help="override the model id; discovered from the provider when omitted",
    )
    parser.add_argument(
        "--agent-budget", type=int, default=40,
        help="how many residual cases the agent may investigate",
    )
    parser.add_argument("--tolerance", type=int, default=5,
                        help="amount tolerance in paise")
    parser.add_argument("--severity", default="medium",
                        choices=[s.value for s in Severity])
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--json", type=Path, help="also write the summary as JSON")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.command == "serve":
        from server.app import serve

        return serve(args.data, port=args.port, adjudicator=args.adjudicator)

    if not args.data.exists():
        raise SystemExit(
            f"no data at {args.data}. Generate a batch first:\n"
            f"  python -m datagen.generate --cases 900 --out {args.data}"
        )

    if args.adjudicator == "agent":
        import os

        model = args.model or os.environ.get(
            f"{args.provider.upper()}_MODEL"
        ) or None
        result = run(
            args.data,
            config=ReconConfig(amount_tolerance=args.tolerance),
            adjudicator_factory=_build_agent_factory(
                args.provider, model, args.agent_budget
            ),
        )
    else:
        result = run(
            args.data,
            config=ReconConfig(amount_tolerance=args.tolerance),
            adjudicator=_build_adjudicator(args.adjudicator),
        )

    if args.command == "recon":
        _print_summary(result)
    elif args.command == "exceptions":
        _print_exceptions(result, Severity(args.severity), args.limit)
    else:
        _print_journal(result, args.limit)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result.summary(), indent=2, default=str), encoding="utf-8"
        )
        print(f"\nsummary written to {args.json}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
