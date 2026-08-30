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

from finctl.config import load_dotenv
from finctl.engine.reconcile import ReconConfig
from finctl.models import Severity
from finctl.money import format_inr, paise_to_rupees
from finctl.pipeline import RunResult, run

#: The React build output. It is committed to the repository on purpose: the
#: frontend is developed with Vite, React and TypeScript, but a reviewer must
#: be able to clone and run `python -m finctl serve` without Node installed.
#: Shipping the build keeps the modern toolchain for developers and the
#: zero-install promise for everyone else.
STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"

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

    def _agent_factory(self):
        """The agent needs the batch, so it is built after ingest.

        Falls back to the offline reasoner when no key is configured, so the
        dashboard always renders something rather than erroring.
        """
        def factory(batch):
            from finctl.adjudicate.agent import AgentAdjudicator

            try:
                return AgentAdjudicator(batch, provider="groq", case_budget=25)
            except RuntimeError:
                from finctl.adjudicate.offline import OfflineAdjudicator

                return OfflineAdjudicator()

        return factory

    def refresh(self) -> None:
        with self.lock:
            try:
                agent_mode = self.adjudicator_kind == "agent"
                self.result = run(
                    self.data_dir,
                    config=ReconConfig(),
                    adjudicator=None if agent_mode else self._adjudicator(),
                    adjudicator_factory=self._agent_factory() if agent_mode else None,
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


def daily_series(result: RunResult) -> list[dict[str, Any]]:
    """Matched vs unresolved value per settlement date.

    Reconciliation is a daily rhythm, and a break usually clusters on one
    day rather than spreading evenly -- a feed that failed, a batch that did
    not decompose. A per-day series makes that shape visible in a way the
    totals never can.
    """
    matched: dict[str, int] = {}
    broken: dict[str, int] = {}

    settlements = result.batch.index_settlements()
    for match in result.matches:
        for settlement_id in match.settlement_ids:
            if settlement := settlements.get(settlement_id):
                key = settlement.settled_on.isoformat()
                matched[key] = matched.get(key, 0) + settlement.amount
    for exception in result.exceptions:
        key = exception.as_of.isoformat()
        broken[key] = broken.get(key, 0) + exception.amount

    days = sorted(set(matched) | set(broken))
    return [
        {
            "date": day,
            "matched": matched.get(day, 0),
            "broken": broken.get(day, 0),
            "matched_display": format_inr(matched.get(day, 0)),
            "broken_display": format_inr(broken.get(day, 0)),
        }
        for day in days
    ]

def exposure_by_reason(result: RunResult) -> list[dict[str, Any]]:
    """Value at risk grouped by reason, worst first.

    Counting breaks treats a one-rupee rounding query the same as a five
    lakh double-post. Ranking by money is what tells an operator where to
    start.
    """
    totals: dict[str, dict[str, Any]] = {}
    for exception in result.exceptions:
        entry = totals.setdefault(
            exception.reason.value,
            {"reason": exception.reason.value, "count": 0, "amount": 0,
             "severity": exception.severity.value},
        )
        entry["count"] += 1
        entry["amount"] += exception.amount
        # Keep the worst severity seen for this reason.
        if Severity(exception.severity.value).rank < Severity(entry["severity"]).rank:
            entry["severity"] = exception.severity.value

    rows = sorted(totals.values(), key=lambda r: -r["amount"])
    for row in rows:
        row["amount_display"] = format_inr(row["amount"])
    return rows


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

    def _serve_app(self) -> None:
        """Serve the built dashboard, or say plainly how to build it.

        The build is committed, so this only fails in a working tree where it
        was deliberately removed. Explaining the two commands beats a bare 404
        that leaves someone guessing which half of the project is missing.
        """
        if (STATIC_DIR / "index.html").is_file():
            self._static("index.html")
            return

        self._send(
            b"<!doctype html><meta charset='utf-8'>"
            b"<style>body{font:14px system-ui;margin:60px auto;max-width:34rem;"
            b"line-height:1.6;color:#e9edf5;background:#0a0c10}"
            b"code{background:#1b202b;padding:2px 6px;border-radius:5px}</style>"
            b"<h2>The dashboard build is missing</h2>"
            b"<p>The API is running and every <code>/api/*</code> endpoint works. "
            b"Only the compiled frontend is absent.</p>"
            b"<p>Build it with:</p>"
            b"<pre><code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code></pre>"
            b"<p>Or use the terminal report instead: "
            b"<code>python -m finctl recon --data data</code></p>",
            "text/html; charset=utf-8",
            503,
        )

    def _static(self, name: str) -> None:
        # Resolve inside the static directory only, so a crafted path cannot
        # read arbitrary files off the machine running the demo.
        root = STATIC_DIR.resolve()
        target = (root / name).resolve()
        # `parents` alone would reject a file sitting directly in the root, so
        # both cases are checked. Either way the path must stay inside the
        # build directory: a crafted URL must not read the machine's files.
        if not target.is_file() or not (target.parent == root or root in target.parents):
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
                self._serve_app()
            elif route.startswith("/assets/"):
                self._static(route[len("/"):])
            elif route.startswith("/static/"):
                # Retained so an older bookmark still resolves.
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
            elif route == "/api/agent":
                self._json(self._agent_payload())
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
        summary["daily"] = daily_series(result)
        summary["exposure_by_reason"] = exposure_by_reason(result)
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

        # Paged so the register stays responsive on a large batch. The client
        # appends rather than replacing, which keeps scroll position stable.
        limit = min(int(query.get("limit", ["60"])[0]), 1000)
        offset = max(int(query.get("offset", ["0"])[0]), 0)
        window = items[offset : offset + limit]

        return {
            "total": len(items),
            "offset": offset,
            "returned": len(window),
            "has_more": offset + len(window) < len(items),
            "total_value": sum(e.amount for e in items),
            "total_value_display": format_inr(sum(e.amount for e in items)),
            "severity_counts": {
                severity.value: sum(1 for e in items if e.severity is severity)
                for severity in Severity
            },
            "items": [_exception_payload(e) for e in window],
        }

    def _exception_detail(self, exception_id: str) -> dict[str, Any]:
        result = self.state.require()
        for exception in result.exceptions:
            if exception.exception_id == exception_id:
                payload = _exception_payload(exception, detailed=True)
                payload["records"] = self._related_records(
                    list(exception.subject_ids) + list(exception.candidates)
                )
                # What the agent actually did, if it looked at this one.
                traces = result.agent_traces
                payload["agent_trace"] = next(
                    (traces[sid] for sid in exception.subject_ids if sid in traces), []
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

    def _agent_payload(self) -> dict[str, Any]:
        """The agent's work, live if it ran, recorded otherwise.

        The distinction is stated in the payload rather than hidden, because a
        recording presented as a live run would be exactly the kind of quiet
        dishonesty this project is built to avoid.
        """
        from finctl.adjudicate.tools import Toolbox
        from finctl.capture_agent import load_transcripts

        result = self.state.require()
        tools = [
            {
                "name": entry["function"]["name"],
                "description": entry["function"]["description"],
            }
            for entry in Toolbox.schema()
        ]

        if result.agent_usage and result.agent_traces:
            return {
                "mode": "live",
                "provider": result.recon.adjudicator_name,
                "model": result.recon.adjudicator_name,
                "usage": result.agent_usage,
                "tools": tools,
                "cases": [],
                "note": "This run executed the agent live against the configured "
                        "provider.",
            }

        recorded = load_transcripts(self.state.data_dir)
        if recorded:
            return {
                "mode": "recorded",
                "provider": recorded.get("provider"),
                "model": recorded.get("model"),
                "recorded_at": recorded.get("recorded_at"),
                "usage": recorded.get("usage", {}),
                "tools": tools,
                "cases": recorded.get("cases", []),
                "note": "Recorded from a real run against a live model. Nothing here "
                        "is simulated. Set GROQ_API_KEY and restart with "
                        "--adjudicator agent to run it yourself.",
            }

        return {
            "mode": "unavailable",
            "tools": tools,
            "cases": [],
            "usage": {},
            "note": "No agent run is available. Set GROQ_API_KEY and restart with "
                    "--adjudicator agent, or run `python -m finctl capture`.",
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
    load_dotenv()
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
