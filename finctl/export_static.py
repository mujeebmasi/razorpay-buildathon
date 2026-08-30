"""Freeze a run to JSON so the dashboard can be hosted as static files.

A reconciliation run is a batch job: it reads a fixed set of files and produces
a fixed result. Nothing about displaying that result needs a server. So rather
than porting the API to serverless functions -- which would mean re-running the
whole cascade per request, or bolting a cache onto something that has no reason
to be dynamic -- the run is computed once and written out as JSON.

The files mirror the live API's routes one for one, so the frontend needs a
loader rather than a rewrite: `/api/run` becomes `data/run.json`, and the
filtering the server did on query parameters happens in the browser instead.
There are 367 exceptions and 746 matches; filtering that client-side is
instant, and it removes an entire tier of infrastructure.

    python -m finctl export --data data --out web/public/data

The output is committed for the same reason `web/dist` is: a reviewer, and a
static host, should not need Python to look at the result.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from finctl.capture_agent import load_transcripts
from finctl.money import format_inr
from finctl.pipeline import RunResult, run as run_pipeline
from finctl.post.journal import trial_balance_report


def _encode(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def _evidence(items) -> list[dict[str, Any]]:
    return [
        {"kind": i.kind, "detail": i.detail, "weight": i.weight,
         "records": list(i.record_ids)}
        for i in items
    ]


def _exceptions(result: RunResult) -> list[dict[str, Any]]:
    """Every exception, with the detail the drawer needs already attached.

    The live API serves summaries and fetches detail per row. Statically that
    would be 367 files, so detail is inlined instead -- it costs a couple of
    hundred kilobytes once and removes a request per interaction.
    """
    settlements = result.batch.index_settlements()
    lines = result.batch.index_bank_lines()
    payments = result.batch.index_payments()
    traces = result.agent_traces

    rows = []
    for exception in result.exceptions:
        records = []
        for record_id in (list(exception.subject_ids) + list(exception.candidates))[:12]:
            if settlement := settlements.get(record_id):
                records.append({
                    "kind": "payout", "id": record_id,
                    "amount": format_inr(settlement.amount),
                    "date": settlement.settled_on.isoformat(),
                    "detail": f"reference {settlement.utr or 'absent'}, "
                              f"{len(settlement.payment_ids)} payment(s)",
                })
            elif line := lines.get(record_id):
                records.append({
                    "kind": "bank line", "id": record_id,
                    "amount": format_inr(line.amount),
                    "date": line.value_date.isoformat(),
                    "detail": line.narration,
                })
            elif payment := payments.get(record_id):
                records.append({
                    "kind": "payment", "id": record_id,
                    "amount": format_inr(payment.gross),
                    "date": payment.captured_on.isoformat(),
                    "detail": f"{payment.method}, fee {format_inr(payment.fee_bearing)}",
                })

        rows.append({
            "id": exception.exception_id,
            "reason": exception.reason.value,
            "severity": exception.severity.value,
            "source": exception.subject_source.value,
            "subjects": list(exception.subject_ids),
            "amount": exception.amount,
            "amount_display": format_inr(exception.amount),
            "as_of": exception.as_of.isoformat(),
            "summary": exception.summary,
            "owner": exception.owner,
            "action": exception.suggested_action,
            "delta": exception.delta,
            "candidate_count": len(exception.candidates),
            "candidates": list(exception.candidates),
            "evidence": _evidence(exception.evidence),
            "records": records,
            "agent_trace": next(
                (traces[s] for s in exception.subject_ids if s in traces), []
            ),
        })
    return rows


def _matches(result: RunResult) -> list[dict[str, Any]]:
    return [
        {
            "id": m.match_id,
            "tier": m.tier.value,
            "reason": m.reason.value,
            "confidence": round(m.confidence, 3),
            "bank_lines": list(m.bank_line_ids),
            "settlements": list(m.settlement_ids),
            "payment_count": len(m.payment_ids),
            "bank_total": m.bank_total,
            "bank_total_display": format_inr(m.bank_total),
            "residual": m.residual,
            "adjudicator": m.adjudicator,
            "rationale": m.rationale,
            "evidence": _evidence(m.evidence),
        }
        for m in result.matches
    ]


def _journal(result: RunResult) -> dict[str, Any]:
    posting = result.posting
    return {
        "balanced": posting.balances,
        "debits": format_inr(posting.total_debits),
        "credits": format_inr(posting.total_credits),
        "entry_count": len(posting.entries),
        "trial_balance": [
            {"account": a, "amount": format_inr(abs(n)),
             "direction": "Dr" if n >= 0 else "Cr", "raw": n}
            for a, n in posting.account_totals().items()
        ],
        "entries": [
            {
                "id": e.entry_id,
                "date": e.posted_on.isoformat(),
                "narrative": e.narrative,
                "lines": [
                    {"account": l.account, "direction": "Dr" if l.debit else "Cr",
                     "amount": format_inr(l.amount), "memo": l.memo}
                    for l in e.lines
                ],
            }
            for e in posting.entries[:60]
        ],
    }


def _scenarios(result: RunResult) -> dict[str, Any]:
    from datagen.scenarios import CATALOGUE

    card = result.scorecard
    by_scenario = card.by_scenario if card else {}
    good = {"correct_match", "correct_exception", "correct_ignore"}
    return {
        "scenarios": [
            {
                "key": s.key, "title": s.title, "description": s.description,
                "difficulty": s.difficulty, "disposition": s.disposition.value,
                "expected_reason": s.expected_reason,
                "cases": sum(by_scenario.get(s.key, {}).values()),
                "correct": sum(
                    n for v, n in by_scenario.get(s.key, {}).items() if v in good
                ),
                "verdicts": by_scenario.get(s.key, {}),
            }
            for s in CATALOGUE
        ]
    }


def _agent(result: RunResult, data_dir: Path) -> dict[str, Any]:
    from finctl.adjudicate.tools import Toolbox

    tools = [
        {"name": e["function"]["name"], "description": e["function"]["description"]}
        for e in Toolbox.schema()
    ]
    recorded = load_transcripts(data_dir)
    if not recorded:
        return {"mode": "unavailable", "tools": tools, "cases": [], "usage": {},
                "note": "No agent recording was exported."}
    return {
        "mode": "recorded",
        "provider": recorded.get("provider"),
        "model": recorded.get("model"),
        "recorded_at": recorded.get("recorded_at"),
        "usage": recorded.get("usage", {}),
        "tools": tools,
        "cases": recorded.get("cases", []),
        "note": "Recorded from a real run against a live model. Nothing here is "
                "simulated. Clone the repo and run with --adjudicator agent to "
                "reproduce it.",
    }


def export(data_dir: Path, out_dir: Path) -> int:
    """Compute a run and write every view of it as JSON."""
    print(f"reconciling {data_dir} ...")
    result = run_pipeline(data_dir, adjudicator=None)
    print(
        f"  {result.records:,} records in {result.total_seconds:.2f}s -> "
        f"{len(result.matches):,} matches, {len(result.exceptions):,} exceptions"
    )

    summary = result.summary()
    summary["value_matched_display"] = format_inr(result.value_matched)
    summary["value_at_risk_display"] = format_inr(result.value_at_risk)
    summary["journal_debits_display"] = format_inr(result.posting.total_debits)
    summary["journal_credits_display"] = format_inr(result.posting.total_credits)
    summary["reserve_display"] = format_inr(result.posting.reserve_total)
    summary["rounding_display"] = format_inr(result.posting.rounding_total)
    summary["fee_recovery_display"] = format_inr(
        result.recon.counters.get("fee_overcharge_paise", 0)
    )
    summary["adjudicator_kind"] = "deterministic cascade (static export)"
    summary["violations"] = result.verification.violations_by_invariant()
    summary["rejected_examples"] = [
        {"match": v.match_id, "invariant": v.invariant.value,
         "detail": v.detail, "adjudicated": v.adjudicated}
        for v in result.verification.violations[:6]
    ]

    # The live API computes these; a static export must carry them too, or the
    # overview loses its chart and its ranked exposure.
    from server.app import daily_series, exposure_by_reason

    summary["daily"] = daily_series(result)
    summary["exposure_by_reason"] = exposure_by_reason(result)

    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "run.json": summary,
        "exceptions.json": _exceptions(result),
        "matches.json": _matches(result),
        "journal.json": _journal(result),
        "scenarios.json": _scenarios(result),
        "agent.json": _agent(result, data_dir),
    }

    total = 0
    for name, payload in files.items():
        path = out_dir / name
        path.write_text(
            json.dumps(payload, default=_encode, ensure_ascii=False),
            encoding="utf-8",
        )
        size = path.stat().st_size
        total += size
        print(f"  {name:<18} {size / 1024:>8.1f} KB")

    print(f"  {'total':<18} {total / 1024:>8.1f} KB  -> {out_dir}")
    return 0
