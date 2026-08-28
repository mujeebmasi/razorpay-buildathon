"""The hosted adjudicator: Claude, constrained and then checked.

Not active in the default configuration -- it requires ANTHROPIC_API_KEY -- but
it is real code on the same interface as the local reasoner, so switching is a
flag rather than a rewrite. It is written to the stdlib so the project keeps its
zero-dependency property either way.

The design principle is that a language model is used for the one thing it is
better at than a scoring function -- weighing heterogeneous, partly-textual
evidence and saying *why* -- and is prevented from doing anything else:

  * It never sees raw money. Amounts are pre-formatted and the arithmetic is
    pre-computed, so it is choosing between candidates rather than doing sums.
  * It can only return an id from the candidate set it was given. An id that
    was not offered is discarded, not looked up.
  * Its output is parsed as strict JSON against a fixed schema. Prose outside
    that schema is ignored rather than interpreted.
  * Whatever it returns still goes through the verifier, which will reject a
    proposal that does not balance regardless of how confident the model was.

Abstention is made explicitly available and explicitly costless in the prompt,
because a model that believes it must answer will always produce one.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Final

from finctl.engine.reconcile import AdjudicationRequest, AdjudicationResult
from finctl.models import Evidence
from finctl.money import format_inr

API_URL: Final[str] = "https://api.anthropic.com/v1/messages"
API_VERSION: Final[str] = "2023-06-01"
DEFAULT_MODEL: Final[str] = "claude-sonnet-5"

SYSTEM_PROMPT: Final[str] = """\
You are a settlement reconciliation analyst. You are given one payout that an \
automated cascade could not match, and every bank credit that could plausibly \
correspond to it, each with pre-computed evidence.

Decide which single credit corresponds to the payout, or decline to decide.

Rules:
- Choose only from the candidate ids you are given. Never invent an id.
- Declining is a correct and expected answer. If two candidates are supported \
about equally, or the evidence is weak, decline. There is no penalty for \
declining and a real cost to a wrong match: a human must first discover it.
- A recovered payment reference is the strongest evidence. An exact amount is \
strong corroboration. Proximity in date is weak on its own.
- A credit differing from the payout by a factor of 100, or by transposed \
digits, indicates a data error, not a match. Decline on those.
- Your reasoning must cite the specific evidence fields you relied on.

Reply with a single JSON object and nothing else:
{"decision": "match" | "abstain", "candidate_id": "<id or null>", \
"confidence": <0.0-1.0>, "reasoning": "<two sentences citing the evidence>"}"""


class ClaudeAdjudicator:
    """Adjudicates via the Anthropic Messages API."""

    kind = "hosted language model"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
        max_tokens: int = 512,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Run with --adjudicator local to use "
                "the offline reasoner instead."
            )
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.name = f"claude/{model}"
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    # -- prompt construction ---------------------------------------------

    def _render(self, request: AdjudicationRequest) -> str:
        """Lay the evidence out as a table.

        Amounts are pre-formatted and deltas pre-computed deliberately: the
        model's job is to weigh evidence, and handing it raw integers invites
        it to attempt arithmetic that the deterministic layer has already done
        exactly.
        """
        lines = [
            f"PAYOUT: {request.subject_description}",
            f"Amount: {format_inr(request.subject_amount)}",
            f"Date:   {request.subject_date}",
            "",
            f"CANDIDATE CREDITS ({len(request.candidates)}):",
        ]
        for index, candidate in enumerate(request.candidates, start=1):
            delta = int(candidate.get("delta", 0))
            if delta == 0:
                gap = "exact match"
            else:
                gap = f"{format_inr(abs(delta))} {'over' if delta > 0 else 'short'}"
            reference_score = float(candidate.get("reference_score", 0.0))

            lines.extend([
                "",
                f"[{index}] id: {candidate['id']}",
                f"    amount:     {format_inr(int(candidate['amount']))} ({gap})",
                f"    value date: {candidate['date']}"
                f" ({candidate.get('banking_days_late', 0)} banking days from payout)",
                f"    reference:  {candidate.get('reference_mechanism', 'none')}"
                f" (score {reference_score:.2f})",
                f"    narration:  {str(candidate.get('narration', ''))[:120]}",
            ])
            if candidate.get("scale_error"):
                lines.append("    WARNING: differs from the payout by exactly 100x")
            if candidate.get("transposition"):
                lines.append("    WARNING: same digits in a different order")
        return "\n".join(lines)

    # -- transport ---------------------------------------------------------

    def _call(self, prompt: str) -> str:
        payload = json.dumps({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        request = urllib.request.Request(
            API_URL,
            data=payload,
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))

        self.calls += 1
        usage = body.get("usage", {})
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)

        return "".join(
            block.get("text", "")
            for block in body.get("content", [])
            if block.get("type") == "text"
        )

    @staticmethod
    def _parse(text: str) -> dict[str, Any] | None:
        """Extract the JSON object, tolerating a fenced or prefixed reply.

        Anything that is not parseable JSON with the expected shape is treated
        as no answer at all. Salvaging intent from malformed output is exactly
        where a guardrail gets quietly bypassed.
        """
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = candidate.split("```")[1]
            if candidate.startswith("json"):
                candidate = candidate[4:]
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    # -- the interface -----------------------------------------------------

    def adjudicate(self, request: AdjudicationRequest) -> AdjudicationResult:
        if not request.candidates:
            return AdjudicationResult(
                decision="abstain", chosen_ids=(), confidence=0.0,
                rationale="no candidate credits fell within the settlement window",
            )

        try:
            raw = self._call(self._render(request))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # A transport failure must never become a match. It becomes an
            # abstention, and the record stays on the exception register.
            return AdjudicationResult(
                decision="abstain", chosen_ids=(), confidence=0.0,
                rationale=f"adjudicator unreachable ({exc}); left for manual review",
            )

        parsed = self._parse(raw)
        if parsed is None:
            return AdjudicationResult(
                decision="abstain", chosen_ids=(), confidence=0.0,
                rationale="adjudicator reply could not be parsed as a decision",
            )

        reasoning = str(parsed.get("reasoning", "")).strip()
        if parsed.get("decision") != "match":
            return AdjudicationResult(
                decision="abstain", chosen_ids=(), confidence=0.0,
                rationale=reasoning or "adjudicator declined to decide",
            )

        chosen = parsed.get("candidate_id")
        offered = {candidate["id"] for candidate in request.candidates}
        if chosen not in offered:
            # The model named something it was not given. Discard rather than
            # resolve: an id that was not on the list is a fabrication.
            return AdjudicationResult(
                decision="abstain", chosen_ids=(), confidence=0.0,
                rationale=(
                    f"adjudicator returned id {chosen!r}, which was not among the "
                    f"candidates offered; the proposal was discarded"
                ),
            )

        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0

        return AdjudicationResult(
            decision="match",
            chosen_ids=(chosen,),
            confidence=confidence,
            rationale=reasoning or "adjudicator matched without stating a reason",
            evidence=(
                Evidence(
                    kind="adjudicator_reasoning",
                    detail=reasoning[:500] or "no reasoning supplied",
                    weight=confidence,
                    record_ids=(request.subject_id, str(chosen)),
                ),
            ),
        )

    @property
    def usage(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
