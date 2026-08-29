"""The investigation toolbox an agent is given over one unresolved payout.

Every tool here is a primitive the deterministic cascade already uses on its
own: reference scoring, gap classification, rate-card inversion, window
listing. The agent does not get new powers, it gets the *same instruments* and
the freedom to combine them in an order no fixed pass would.

Three rules shape the surface.

**Read-only.** Nothing here mutates the batch or claims a record. The agent
investigates and reports; the cascade does the claiming and the verifier does
the accepting. A tool that could write would put the agent inside the trust
boundary instead of outside it.

**Scoped to one subject.** Each toolbox is constructed around a single payout,
and the candidate set is fixed at construction. The agent cannot wander into
records that were never offered, which is what makes "it chose an id it was
never given" a detectable fabrication rather than a plausible answer.

**Pre-computed arithmetic.** Amounts come back formatted and gaps come back
already classified. The model is being asked to weigh evidence, not to do
mental arithmetic on crores of paise -- it is bad at that, and the engine is
exact at it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Sequence

from finctl.engine.feemodel import invert_across_methods
from finctl.engine.narration import score_reference_match
from finctl.ingest.loader import Batch
from finctl.money import digit_transposition, format_inr, looks_like_scale_error
from finctl.timeutil import banking_days_between


@dataclass(slots=True)
class ToolCall:
    """One invocation, kept for the evidence trail."""

    name: str
    arguments: dict[str, Any]
    result: str

    def summary(self) -> str:
        arguments = ", ".join(f"{k}={v}" for k, v in self.arguments.items())
        return f"{self.name}({arguments})"


@dataclass(slots=True)
class Toolbox:
    """Read-only investigation surface bound to one payout and its candidates."""

    batch: Batch
    settlement_id: str
    candidate_ids: tuple[str, ...]
    calls: list[ToolCall] = field(default_factory=list)

    # -- schema handed to the model ---------------------------------------

    @staticmethod
    def schema() -> list[dict[str, Any]]:
        """OpenAI-compatible function definitions.

        Descriptions are written for the model, not for a developer: they say
        when to reach for the tool, because a tool the model does not know the
        purpose of is a tool it never calls.
        """
        def fn(name: str, description: str, properties: dict, required: list[str]):
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }

        line = {"line_id": {"type": "string", "description": "A candidate bank credit id."}}

        return [
            fn("get_credit",
               "Full detail of one candidate bank credit: amount, value date, and the "
               "complete bank narration text. Call this first on any candidate you are "
               "seriously considering.",
               line, ["line_id"]),

            fn("scan_references",
               "Score the payout's reference against EVERY candidate at once and "
               "return them ranked. Start here whenever the payout has a reference -- "
               "it answers 'which of these carries it?' in one call, instead of "
               "checking candidates one at a time.",
               {}, []),

            fn("score_reference",
               "Check whether the payout's reference number appears in a candidate's "
               "narration. Returns how it matched -- printed in full, truncated by the "
               "bank, or a near miss -- and a score from 0 to 1. This is the strongest "
               "single piece of evidence available.",
               line, ["line_id"]),

            fn("explain_gap",
               "Compare the payout amount against a candidate's amount. Returns the "
               "difference and whether it looks like ordinary rounding, a 100x unit "
               "error, or transposed digits. Use this before concluding two amounts "
               "'nearly match'.",
               line, ["line_id"]),

            fn("payout_components",
               "The payments, refunds and adjustments the PSP says make up this payout, "
               "and whether they sum to it. Use this when a credit is smaller than the "
               "payout and you need to know if a deduction explains it.",
               {}, []),

            fn("invert_fee",
               "Given a net amount, return the gross payment amounts that could have "
               "produced it under the gateway's rate card, per payment method. Use this "
               "to test whether a credit is consistent with being the net of a real "
               "payment rather than an unrelated transfer.",
               {"net_amount_paise": {"type": "integer",
                                     "description": "Net amount in paise."}},
               ["net_amount_paise"]),

            fn("list_credits_near",
               "List candidate credits within a number of days of the payout date, with "
               "amounts. Use this to judge whether a candidate is uniquely plausible or "
               "one of several lookalikes.",
               {"days": {"type": "integer",
                         "description": "Half-width of the window in days, 0 to 10."}},
               ["days"]),

            fn("check_contested",
               "Ask whether OTHER payouts in the same window would fit this credit just "
               "as well. Always call this before matching on amount alone. If another "
               "payout of the same value is also unmatched, the credit does not identify "
               "either of them and you must decline -- choosing one would be a coin flip.",
               line, ["line_id"]),
        ]

    # -- dispatch ----------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Run a tool and record it. Unknown names are reported, never raised."""
        handler: Callable[..., Any] | None = {
            "get_credit": self._get_credit,
            "scan_references": self._scan_references,
            "score_reference": self._score_reference,
            "explain_gap": self._explain_gap,
            "payout_components": self._payout_components,
            "invert_fee": self._invert_fee,
            "list_credits_near": self._list_credits_near,
            "check_contested": self._check_contested,
        }.get(name)

        if handler is None:
            result = json.dumps({"error": f"no such tool: {name}"})
        else:
            try:
                result = json.dumps(handler(**arguments), default=str)
            except TypeError as exc:
                result = json.dumps({"error": f"bad arguments for {name}: {exc}"})
            except Exception as exc:  # a tool fault must not kill the run
                result = json.dumps({"error": f"{type(exc).__name__}: {exc}"})

        self.calls.append(ToolCall(name=name, arguments=dict(arguments), result=result))
        return result

    # -- helpers -----------------------------------------------------------

    @property
    def _settlement(self):
        return self.batch.index_settlements()[self.settlement_id]

    def _guard(self, line_id: str):
        """Candidates are fixed at construction; anything else is out of scope."""
        if line_id not in self.candidate_ids:
            return None
        return self.batch.index_bank_lines().get(line_id)

    # -- tools -------------------------------------------------------------

    def _get_credit(self, line_id: str) -> dict[str, Any]:
        line = self._guard(line_id)
        if line is None:
            return {"error": f"{line_id} is not one of the candidates offered"}
        return {
            "line_id": line.line_id,
            "amount": format_inr(line.amount),
            "value_date": line.value_date.isoformat(),
            "narration": line.narration,
            "banking_days_from_payout": banking_days_between(
                self._settlement.settled_on, line.value_date
            ),
        }

    def _scan_references(self) -> dict[str, Any]:
        """Score the reference against every candidate in one pass.

        Checking candidates one at a time is how an agent exhausts its turn
        budget on a wide candidate set and concludes nothing. The deterministic
        cascade never scans linearly either -- it indexes -- so this exposes the
        same shape of answer.
        """
        reference = self._settlement.utr
        if not reference:
            return {
                "reference": None,
                "verdict": "this payout carries no reference, so narration cannot "
                           "confirm or deny any candidate. Judge on amount, and call "
                           "check_contested before matching on amount alone.",
            }

        lines = self.batch.index_bank_lines()
        scored = []
        for line_id in self.candidate_ids:
            line = lines.get(line_id)
            if line is None:
                continue
            score, mechanism, _ = score_reference_match(reference, line.narration)
            scored.append({
                "line_id": line_id,
                "score": round(score, 3),
                "mechanism": mechanism,
                "amount": format_inr(line.amount),
            })

        scored.sort(key=lambda row: (-row["score"], row["line_id"]))
        best = scored[0] if scored else None
        hits = [row for row in scored if row["score"] > 0]

        return {
            "reference": reference,
            "candidates_scanned": len(scored),
            "candidates_carrying_it": len(hits),
            "ranked": scored[:8],
            "verdict": (
                f"{best['line_id']} carries the reference ({best['mechanism']}, "
                f"score {best['score']})" if best and best["score"] >= 0.75
                else "no candidate carries this reference; judge on amount instead, "
                     "and call check_contested before matching on amount alone"
            ),
        }

    def _score_reference(self, line_id: str) -> dict[str, Any]:
        line = self._guard(line_id)
        if line is None:
            return {"error": f"{line_id} is not one of the candidates offered"}
        reference = self._settlement.utr
        if not reference:
            return {
                "reference": None,
                "verdict": "this payout carries no reference number, so narration "
                           "cannot confirm or deny any candidate",
            }
        score, mechanism, token = score_reference_match(reference, line.narration)
        return {
            "reference": reference,
            "score": round(score, 3),
            "mechanism": mechanism,
            "matched_token": token or None,
            "verdict": (
                "decisive match" if score >= 0.95
                else "strong but not exact" if score >= 0.75
                else "weak" if score > 0
                else "no trace of the reference in this narration"
            ),
        }

    def _explain_gap(self, line_id: str) -> dict[str, Any]:
        line = self._guard(line_id)
        if line is None:
            return {"error": f"{line_id} is not one of the candidates offered"}
        payout = self._settlement.amount
        delta = line.amount - payout
        return {
            "payout_amount": format_inr(payout),
            "credit_amount": format_inr(line.amount),
            "difference": format_inr(abs(delta)),
            "credit_is": "larger" if delta > 0 else "smaller" if delta < 0 else "identical",
            "within_rounding_tolerance": abs(delta) <= 5,
            "looks_like_100x_unit_error": looks_like_scale_error(line.amount, payout),
            "looks_like_transposed_digits": digit_transposition(line.amount, payout),
        }

    def _payout_components(self) -> dict[str, Any]:
        settlement = self._settlement
        payments = self.batch.index_payments()
        refunds = {r.refund_id: r for r in self.batch.refunds}
        adjustments = {a.adjustment_id: a for a in self.batch.adjustments}

        known = [pid for pid in settlement.payment_ids if pid in payments]
        net = sum(payments[pid].net for pid in known)
        refunded = sum(refunds[r].amount for r in settlement.refund_ids if r in refunds)
        adjusted = sum(
            adjustments[a].amount for a in settlement.adjustment_ids if a in adjustments
        )
        expected = net - refunded + adjusted

        return {
            "payment_count": len(known),
            "payments_missing_from_batch": len(settlement.payment_ids) - len(known),
            "payments_net_total": format_inr(net),
            "refunds_deducted": format_inr(refunded),
            "adjustments": format_inr(adjusted),
            "components_total": format_inr(expected),
            "payout_states": format_inr(settlement.amount),
            "components_explain_the_payout": abs(settlement.amount - expected) <= 5,
            "unexplained_difference": format_inr(abs(settlement.amount - expected)),
        }

    def _invert_fee(self, net_amount_paise: int) -> dict[str, Any]:
        inversions = invert_across_methods(int(net_amount_paise))
        if not inversions:
            return {
                "net_amount": format_inr(int(net_amount_paise)),
                "verdict": "no gross amount under the rate card produces this net, so "
                           "it is unlikely to be a gateway payout of a single payment",
            }
        return {
            "net_amount": format_inr(int(net_amount_paise)),
            "possible_gross_by_method": {
                method: [format_inr(g) for g in grosses[:3]]
                for method, grosses in list(inversions.items())[:5]
            },
            "verdict": "consistent with being the net of a real payment",
        }

    def _check_contested(self, line_id: str) -> dict[str, Any]:
        """Do other payouts fit this credit equally well?

        This is the mutual-uniqueness rule the deterministic cascade applies at
        its amount-matching tier, exposed so the agent can apply it too. A
        payout having exactly one candidate credit is not sufficient evidence:
        if that credit also fits a second payout, choosing either is a coin
        flip with a confidence score attached to it.
        """
        line = self._guard(line_id)
        if line is None:
            return {"error": f"{line_id} is not one of the candidates offered"}

        anchor = self._settlement.settled_on
        rivals = []
        for settlement in self.batch.settlements:
            if settlement.settlement_id == self.settlement_id:
                continue
            if abs((settlement.settled_on - anchor).days) > 3:
                continue
            if abs(settlement.amount - line.amount) <= 5:
                rivals.append({
                    "settlement_id": settlement.settlement_id,
                    "amount": format_inr(settlement.amount),
                    "settled_on": settlement.settled_on.isoformat(),
                    "has_reference": bool(settlement.utr),
                })

        rivals.sort(key=lambda r: r["settlement_id"])
        return {
            "credit": line_id,
            "credit_amount": format_inr(line.amount),
            "other_payouts_that_fit_equally_well": len(rivals),
            "rivals": rivals[:6],
            "verdict": (
                "uniquely yours -- no other payout in the window matches this credit"
                if not rivals else
                f"CONTESTED -- {len(rivals)} other payout(s) match this credit just as "
                f"well. Matching on amount alone would be a coin flip; decline unless a "
                f"reference distinguishes them."
            ),
        }

    def _list_credits_near(self, days: int) -> dict[str, Any]:
        days = max(0, min(int(days), 10))
        lines = self.batch.index_bank_lines()
        anchor = self._settlement.settled_on
        window = []
        for line_id in self.candidate_ids:
            line = lines.get(line_id)
            if line is None:
                continue
            if abs((line.value_date - anchor).days) <= days:
                window.append({
                    "line_id": line_id,
                    "amount": format_inr(line.amount),
                    "value_date": line.value_date.isoformat(),
                })
        window.sort(key=lambda row: row["line_id"])
        return {
            "payout_amount": format_inr(self._settlement.amount),
            "payout_date": anchor.isoformat(),
            "window_days": days,
            "candidates_in_window": len(window),
            "credits": window[:12],
        }


def build_toolbox(
    batch: Batch, settlement_id: str, candidate_ids: Sequence[str]
) -> Toolbox:
    return Toolbox(
        batch=batch,
        settlement_id=settlement_id,
        candidate_ids=tuple(candidate_ids),
    )
