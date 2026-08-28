"""A dependency-free dashboard server.

`http.server` from the standard library rather than a framework, for the same
reason the rest of the project has no dependencies: the demo has to run on a
machine nobody prepared, and `pip install` failing is not a risk worth taking
for routing that fits in eighty lines.

The run is executed once at startup and held in memory. Reconciliation is a
batch operation, not a per-request one, and re-running it on every page load
would make the dashboard slower than the engine it is displaying.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from finctl.engine.reconcile import ReconConfig
from finctl.models import Severity
from finctl.money import format_inr, paise_to_rupees
from finctl.pipeline import RunResult, run

STATIC_DIR = Path(__file__).resolve().parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _encode(value: Any) -> Any:
    """JSON encoder for the domain types that reach the wire."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


class _State:
    """The run being displayed, rebuildable on demand."""

    def __init__(self, data_dir: Path, adjudicator_kind: str) -> None:
        self.data_dir = data_dir
        self.adjudicator_kind = adjudicator_kind
        self.lock = threading.Lock()
        self.result: RunResult | None = None
        self.error: str | None = None

    def _adjudicator(self):
        if self.adjudicator_kind == "none":
            return None
        if self.adjudicator_kind == "claude":
            try:
                from finctl.adjudicate.claude import ClaudeAdjudicator

                return ClaudeAdjudicator()
            except RuntimeError:
                pass  # no key configured; fall through to the local reasoner
        from finctl.adjudicate.offline import OfflineAdjudicator

        return OfflineAdjudicator()

    def refresh(self) -> None:
        with self.lock:
            try:
                self.result = run(
                    self.data_dir,
                    config=ReconConfig(),
                    adjudicator=self._adjudicator(),
                )
                self.error = None
            except Exception as exc:  # surfaced in the UI rather than a stack trace
                self.result = None
                self.error = f"{type(exc).__name__}: {exc}"

    def require(self) -> RunResult:
        if self.result is None:
            self.refresh()
        if self.result is None:
            raise RuntimeError(self.error or "the run produced no result")
        return self.result


def _exception_payload(exception, *, detailed: bool = False) -> dict[str, Any]:
    payload = {
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
    }
    if detailed:
        payload["candidates"] = list(exception.candidates)
        payload["evidence"] = [
            {"kind": item.kind, "detail": item.detail, "weight": item.weight,
             "records": list(item.record_ids)}
            for item in exception.evidence
        ]
    return payload


def _match_payload(match, *, detailed: bool = False) -> dict[str, Any]:
    payload = {
        "id": match.match_id,
        "tier": match.tier.value,
        "reason": match.reason.value,
        "confidence": round(match.confidence, 3),
        "bank_lines": list(match.bank_line_ids),
        "settlements": list(match.settlement_ids),
        "payment_count": len(match.payment_ids),
        "bank_total": match.bank_total,
        "bank_total_display": format_inr(match.bank_total),
        "residual": match.residual,
        "adjudicator": match.adjudicator,
    }
    if detailed:
        payload["rationale"] = match.rationale
        payload["evidence"] = [
            {"kind": item.kind, "detail": item.detail, "weight": item.weight,
             "records": list(item.record_ids)}
            for item in match.evidence
        ]
    return payload


class Handler(BaseHTTPRequestHandler):
    """Routes the handful of endpoints the dashboard needs."""

    server_version = "finctl"

    def __init__(self, *args, state: _State, **kwargs) -> None:
        self.state = state
        super().__init__(*args, **kwargs)

    # -- plumbing ---------------------------------------------------------

    def log_message(self, *args) -> None:  # noqa: D102 - silence per-request noise
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(
            json.dumps(payload, default=_encode).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _static(self, name: str) -> None:
        # Resolve inside the static directory only, so a crafted path cannot
        # read arbitrary files off the machine running the demo.
        target = (STATIC_DIR / name).resolve()
        if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        self._send(
            target.read_bytes(),
            _CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
        )

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if route == "/":
                self._static("index.html")
            elif route.startswith("/static/"):
                self._static(route[len("/static/"):])
            elif route == "/api/run":
                self._json(self._run_payload())
            elif route == "/api/exceptions":
                self._json(self._exceptions_payload(query))
            elif route.startswith("/api/exception/"):
                self._json(self._exception_detail(route.rsplit("/", 1)[-1]))
            elif route == "/api/matches":
                self._json(self._matches_payload(query))
            elif route == "/api/journal":
                self._json(self._journal_payload(query))
            elif route == "/api/scenarios":
                self._json(self._scenarios_payload())
            elif route == "/api/refresh":
                self.state.refresh()
                self._json({"ok": self.state.error is None, "error": self.state.error})
            else:
                self._json({"error": f"no route {route}"}, 404)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # -- payload builders --------------------------------------------------

    def _run_payload(self) -> dict[str, Any]:
        result = self.state.require()
        summary = result.summary()
        summary["value_matched_display"] = format_inr(result.value_matched)
        summary["value_at_risk_display"] = format_inr(result.value_at_risk)
        summary["journal_debits_display"] = format_inr(result.posting.total_debits)
        summary["journal_credits_display"] = format_inr(result.posting.total_credits)
        summary["reserve_display"] = format_inr(result.posting.reserve_total)
        summary["rounding_display"] = format_inr(result.posting.rounding_total)
        summary["adjudicator_kind"] = self.state.adjudicator_kind
        summary["fee_recovery_display"] = format_inr(
            result.recon.counters.get("fee_overcharge_paise", 0)
        )
        summary["violations"] = result.verification.violations_by_invariant()
        summary["rejected_examples"] = [
            {"match": v.match_id, "invariant": v.invariant.value, "detail": v.detail,
             "adjudicated": v.adjudicated}
            for v in result.verification.violations[:6]
        ]
        return summary

    def _exceptions_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        result = self.state.require()
        items = result.exceptions

        if severity := query.get("severity", [None])[0]:
            if severity != "all":
                ceiling = Severity(severity).rank
                items = [e for e in items if e.severity.rank <= ceiling]
        if reason := query.get("reason", [None])[0]:
            if reason != "all":
                items = [e for e in items if e.reason.value == reason]
        if search := query.get("q", [None])[0]:
            needle = search.lower()
            items = [
                e for e in items
                if needle in e.summary.lower()
                or any(needle in sid.lower() for sid in e.subject_ids)
            ]

        limit = min(int(query.get("limit", ["200"])[0]), 1000)
        return {
            "total": len(items),
            "total_value": sum(e.amount for e in items),
            "total_value_display": format_inr(sum(e.amount for e in items)),
            "items": [_exception_payload(e) for e in items[:limit]],
        }

    def _exception_detail(self, exception_id: str) -> dict[str, Any]:
        result = self.state.require()
        for exception in result.exceptions:
            if exception.exception_id == exception_id:
                payload = _exception_payload(exception, detailed=True)
                payload["records"] = self._related_records(
                    list(exception.subject_ids) + list(exception.candidates)
                )
                return payload
        return {"error": "unknown exception"}

    def _related_records(self, ids: list[str]) -> list[dict[str, Any]]:
        """The underlying rows behind an exception, so the trail is complete."""
        result = self.state.require()
        settlements = result.batch.index_settlements()
        lines = result.batch.index_bank_lines()
        payments = result.batch.index_payments()

        records: list[dict[str, Any]] = []
        for record_id in ids[:12]:
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
        return records

    def _matches_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        result = self.state.require()
        items = result.matches
        if tier := query.get("tier", [None])[0]:
            if tier != "all":
                items = [m for m in items if m.tier.value == tier]
        limit = min(int(query.get("limit", ["120"])[0]), 500)
        detailed = query.get("detail", ["0"])[0] == "1"
        return {
            "total": len(items),
            "items": [_match_payload(m, detailed=detailed) for m in items[:limit]],
        }

    def _journal_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        result = self.state.require()
        posting = result.posting
        limit = min(int(query.get("limit", ["40"])[0]), 400)
        return {
            "balanced": posting.balances,
            "debits": format_inr(posting.total_debits),
            "credits": format_inr(posting.total_credits),
            "entry_count": len(posting.entries),
            "trial_balance": [
                {"account": account, "amount": format_inr(abs(net)),
                 "direction": "Dr" if net >= 0 else "Cr", "raw": net}
                for account, net in posting.account_totals().items()
            ],
            "entries": [
                {
                    "id": entry.entry_id,
                    "date": entry.posted_on.isoformat(),
                    "narrative": entry.narrative,
                    "lines": [
                        {"account": line.account, "direction": "Dr" if line.debit else "Cr",
                         "amount": format_inr(line.amount), "memo": line.memo}
                        for line in entry.lines
                    ],
                }
                for entry in posting.entries[:limit]
            ],
        }

    def _scenarios_payload(self) -> dict[str, Any]:
        from datagen.scenarios import CATALOGUE

        result = self.state.require()
        card = result.scorecard
        by_scenario = card.by_scenario if card else {}

        good = {"correct_match", "correct_exception", "correct_ignore"}
        return {
            "scenarios": [
                {
                    "key": scenario.key,
                    "title": scenario.title,
                    "description": scenario.description,
                    "difficulty": scenario.difficulty,
                    "disposition": scenario.disposition.value,
                    "expected_reason": scenario.expected_reason,
                    "cases": sum(by_scenario.get(scenario.key, {}).values()),
                    "correct": sum(
                        count for verdict, count in by_scenario.get(scenario.key, {}).items()
                        if verdict in good
                    ),
                    "verdicts": by_scenario.get(scenario.key, {}),
                }
                for scenario in CATALOGUE
            ]
        }


def serve(data_dir: Path, *, port: int = 8000, adjudicator: str = "local") -> int:
    """Run the reconciliation once, then serve the dashboard over it."""
    state = _State(data_dir, adjudicator)
    print(f"reconciling {data_dir} ...")
    state.refresh()
    if state.error:
        print(f"error: {state.error}")
        return 1

    result = state.require()
    print(
        f"  {result.records:,} records in {result.total_seconds:.2f}s, "
        f"{len(result.matches):,} matches, {len(result.exceptions):,} exceptions"
    )

    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, state=state))
    print(f"\n  dashboard -> http://127.0.0.1:{port}\n  ctrl-c to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
    return 0
