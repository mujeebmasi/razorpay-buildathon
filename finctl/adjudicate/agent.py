"""A tool-using agent that investigates the residual the cascade could not explain.

This is the component that makes finctl an agent rather than a pipeline with a
scoring function on the end. Given one unresolved payout and a fixed candidate
set, it decides *what to look at next* -- pull a narration, score a reference,
classify a gap, invert the rate card, widen the window -- and only then reaches
a conclusion. The sequence of tool calls is not scripted; it is the agent's.

Written against the **OpenAI-compatible** chat-completions schema, so one
adapter serves Groq, Gemini's compatibility endpoint, OpenAI, OpenRouter or a
local server. Only a base URL, an environment variable and a model id change.
Transport is `urllib` from the standard library, so the project keeps its
zero-dependency property.

Four containment rules, because the agent sits *outside* the trust boundary:

  * It may only choose from the candidate ids it was handed. An id that was
    not offered is discarded as a fabrication, not looked up.
  * It never sees raw paise or does arithmetic. Tools return formatted amounts
    and pre-classified gaps.
  * Any failure -- transport, timeout, malformed reply, exhausted turns --
    degrades to an abstention. A failure can never become a match.
  * Whatever it concludes still goes to the verifier, which recomputes from the
    original records and can veto it.

Determinism: this tier is explicitly non-deterministic, unlike the rest of the
engine. Temperature is pinned to zero and the full tool trace is recorded, so a
decision is *auditable* even where it is not bit-reproducible. The deterministic
cascade is unaffected and remains the default.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Final

from finctl.adjudicate.tools import Toolbox, build_toolbox
from finctl.engine.reconcile import AdjudicationRequest, AdjudicationResult
from finctl.ingest.loader import Batch
from finctl.models import Evidence
from finctl.money import format_inr


class RateLimited(RuntimeError):
    """The host asked us to slow down, and said for how long."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _retry_hint(exc: Any, message: str) -> float:
    """How long to wait, from the header or the message, bounded.

    Providers put the wait in a `retry-after` header, or spell it out in the
    error text ("try again in 2.5s"). Either is better than a fixed sleep, and
    the bound stops a mis-parse from stalling a batch run.
    """
    header = None
    try:
        header = exc.headers.get("retry-after")
    except Exception:
        pass
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass

    found = re.search(r"try again in ([\d.]+)\s*(ms|s)", message, re.I)
    if found:
        value = float(found.group(1))
        return min(value / 1000 if found.group(2).lower() == "ms" else value, 30.0)
    return 4.0


@dataclass(frozen=True, slots=True)
class Provider:
    """Everything that differs between one OpenAI-compatible host and another."""

    name: str
    base_url: str
    key_env: str
    #: Substrings tried in order against the host's live model list. Matching by
    #: substring rather than pinning an exact id means the adapter keeps working
    #: as providers retire and rename models.
    prefer: tuple[str, ...]


PROVIDERS: Final[dict[str, Provider]] = {
    "groq": Provider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        prefer=("llama-3.3-70b", "llama-3.1-70b", "70b", "llama-4", "qwen", "kimi", "gpt-oss"),
    ),
    "gemini": Provider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        prefer=("gemini-2.5-flash", "gemini-2.0-flash", "flash", "gemini-2.5-pro", "pro"),
    ),
    "openai": Provider(
        name="openai",
        base_url="https://api.openai.com/v1",
        key_env="OPENAI_API_KEY",
        prefer=("gpt-4.1-mini", "gpt-4o-mini", "mini", "gpt-4"),
    ),
}


SYSTEM_PROMPT: Final[str] = """\
You are a settlement reconciliation analyst investigating one payout that an \
automated cascade could not match to a bank credit.

You have tools. Use them before deciding — do not guess from the summary alone. \
A typical investigation checks the reference against the strongest candidate, \
classifies the amount gap, and confirms no other candidate fits equally well.

How to weigh what you find:
- A recovered payment reference is the strongest evidence there is.
- An exact amount is strong corroboration. Ordinary rounding is a few paise.
- A 100x difference is a unit bug, and transposed digits are a keying error. \
Neither is a match. Decline on both.
- Closeness in date is weak on its own. Many unrelated credits share a date.
- Before matching on amount alone, call `check_contested`. If another payout \
fits that credit equally well, the credit identifies neither of them, and \
choosing one is a coin flip. Decline.
- If two candidates are supported about equally, you cannot tell them apart. \
Say so.

Declining is a correct and expected answer, and there is no penalty for it. A \
wrong match is far worse than no match: it is invisible in the output and a \
human has to discover it. When the evidence does not single out one candidate, \
decline.

When you are ready, call `submit_decision` exactly once. Cite the specific tool \
results you relied on in your reasoning."""


DECISION_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "submit_decision",
        "description": "Record your final conclusion. Call this exactly once, after "
                       "investigating with the other tools.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["match", "decline"],
                    "description": "'match' only if one candidate is clearly the "
                                   "corresponding credit; otherwise 'decline'.",
                },
                "line_id": {
                    "type": "string",
                    "description": "The chosen candidate id, or empty when declining.",
                },
                "confidence": {
                    "type": "number",
                    "description": "0 to 1.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Two or three sentences citing the tool results you "
                                   "relied on.",
                },
            },
            "required": ["decision", "reasoning"],
        },
    },
}


@dataclass(slots=True)
class AgentUsage:
    """Cost and effort accounting, reported alongside the run."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    seconds: float = 0.0
    decided: int = 0
    declined: int = 0
    failed: int = 0
    throttled: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tool_calls": self.tool_calls,
            "seconds": round(self.seconds, 2),
            "decided": self.decided,
            "declined": self.declined,
            "failed": self.failed,
            "throttled": self.throttled,
        }


class AgentAdjudicator:
    """Investigates, then decides or declines. Always subject to the verifier."""

    kind = "tool-using LLM agent"

    def __init__(
        self,
        batch: Batch,
        *,
        provider: str = "groq",
        model: str | None = None,
        api_key: str | None = None,
        max_turns: int = 6,
        timeout: float = 60.0,
        case_budget: int = 40,
        max_tokens: int = 900,
    ) -> None:
        if provider not in PROVIDERS:
            raise RuntimeError(
                f"unknown provider {provider!r}; choose from {sorted(PROVIDERS)}"
            )
        self.provider = PROVIDERS[provider]
        self.api_key = api_key or os.environ.get(self.provider.key_env, "")
        if not self.api_key:
            raise RuntimeError(
                f"{self.provider.key_env} is not set. Export it in your shell, or run "
                f"with --adjudicator local to use the offline reasoner."
            )

        self.batch = batch
        self.max_turns = max_turns
        self.timeout = timeout
        #: Enough for a reasoning model to think and emit one tool call, and no
        #: more. Hosts meter on *requested* tokens, not tokens used, so an
        #: inflated cap burns the rate limit on output that never arrives.
        self.max_tokens = max_tokens
        #: Investigating every residual case would multiply latency by hundreds of
        #: round trips. The budget bounds it; cases beyond it fall through to the
        #: leftover pass and are reported as breaks, which is the honest outcome
        #: rather than a silently truncated run.
        self.case_budget = case_budget
        self.seen = 0

        # Precedence: explicit argument, then {PROVIDER}_MODEL from the
        # environment, then ask the host what it serves. Discovery is the
        # fallback, not the default -- an operator who named a model meant it.
        self.model = (
            model
            or os.environ.get(f"{self.provider.name.upper()}_MODEL", "").strip()
            or self._discover_model()
        )
        self.name = f"{self.provider.name}/{self.model}"
        self.usage = AgentUsage()
        #: subject id -> the tool calls the agent made, for the evidence trail
        self.traces: dict[str, list[str]] = {}

    # -- transport ---------------------------------------------------------

    def _request(self, path: str, payload: dict | None = None) -> dict:
        url = f"{self.provider.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
                # Required, not cosmetic. Several of these hosts sit behind a
                # WAF that rejects the default "Python-urllib/3.x" agent with a
                # 403 that looks exactly like a bad key. Identifying the client
                # properly is what makes the request go through.
                "user-agent": "finctl/1.0 (settlement reconciliation)",
                "accept": "application/json",
            },
            method="POST" if data else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The provider's own error message is far more useful than the
            # status line, and it never contains the key.
            try:
                with exc:
                    detail = json.loads(exc.read().decode("utf-8"))
                message = (
                    detail.get("error", {}).get("message")
                    if isinstance(detail.get("error"), dict) else None
                ) or str(detail)[:300]
            except Exception:
                message = exc.reason
            if exc.code == 429:
                raise RateLimited(message, retry_after=_retry_hint(exc, message)) from exc
            raise RuntimeError(f"{self.provider.name} HTTP {exc.code}: {message}") from exc

    def _complete(self, payload: dict, attempts: int = 3) -> dict:
        """POST a completion, waiting out a rate limit rather than failing."""
        for attempt in range(attempts):
            try:
                return self._request("/chat/completions", payload)
            except RateLimited as limited:
                if attempt == attempts - 1:
                    raise
                self.usage.throttled += 1
                time.sleep(limited.retry_after)
        raise RuntimeError("unreachable")

    def _discover_model(self) -> str:
        """Ask the host what it serves and pick by preference.

        Pinning an exact model id ages badly -- providers retire and rename them
        constantly. Asking, then matching by substring, means the adapter keeps
        working without a code change.
        """
        try:
            body = self._request("/models")
        except Exception as exc:
            raise RuntimeError(
                f"could not reach {self.provider.name} to list models: {exc}"
            ) from exc

        available = [
            entry["id"] for entry in body.get("data", []) if isinstance(entry, dict)
        ]
        if not available:
            raise RuntimeError(f"{self.provider.name} returned no models")

        for wanted in self.provider.prefer:
            for model_id in available:
                lowered = model_id.lower()
                if wanted in lowered and "whisper" not in lowered and "tts" not in lowered:
                    return model_id
        return available[0]

    # -- the agent loop ----------------------------------------------------

    def _brief(self, request: AdjudicationRequest) -> str:
        lines = [
            f"PAYOUT {request.subject_id}",
            f"  amount:    {format_inr(request.subject_amount)}",
            f"  date:      {request.subject_date}",
            f"  reference: {self._reference(request) or 'none on file'}",
            "",
            f"CANDIDATE CREDITS ({len(request.candidates)}) — you may only choose from these:",
        ]
        for candidate in request.candidates:
            lines.append(
                f"  {candidate['id']}  {format_inr(int(candidate['amount']))}"
                f"  {candidate['date']}"
            )
        lines += ["", "Investigate with the tools, then call submit_decision."]
        return "\n".join(lines)

    def _reference(self, request: AdjudicationRequest) -> str | None:
        settlement = self.batch.index_settlements().get(request.subject_id)
        return settlement.utr if settlement else None

    def adjudicate(self, request: AdjudicationRequest) -> AdjudicationResult:
        if not request.candidates:
            return AdjudicationResult(
                "abstain", (), 0.0,
                "no candidate credits fell within the settlement window",
            )

        self.seen += 1
        if self.seen > self.case_budget:
            return AdjudicationResult(
                "abstain", (), 0.0,
                f"agent case budget of {self.case_budget} exhausted; left for the "
                f"exception register rather than truncating the run silently",
            )

        candidate_ids = [c["id"] for c in request.candidates]
        toolbox = build_toolbox(self.batch, request.subject_id, candidate_ids)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._brief(request)},
        ]
        tools = Toolbox.schema() + [DECISION_TOOL]

        started = time.perf_counter()
        try:
            outcome = self._run_loop(messages, tools, toolbox, candidate_ids)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.usage.failed += 1
            outcome = AdjudicationResult(
                "abstain", (), 0.0,
                f"agent unreachable ({type(exc).__name__}); left for manual review",
            )
        except RuntimeError as exc:
            self.usage.failed += 1
            outcome = AdjudicationResult(
                "abstain", (), 0.0, f"agent call failed: {exc}",
            )
        except Exception as exc:
            self.usage.failed += 1
            outcome = AdjudicationResult(
                "abstain", (), 0.0,
                f"agent error ({type(exc).__name__}: {exc}); left for manual review",
            )
        finally:
            self.usage.seconds += time.perf_counter() - started

        self.traces[request.subject_id] = [c.summary() for c in toolbox.calls]
        self.usage.tool_calls += len(toolbox.calls)
        return outcome

    def _run_loop(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        toolbox: Toolbox,
        candidate_ids: list[str],
    ) -> AdjudicationResult:
        for _ in range(self.max_turns):
            body = self._complete({
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0,
                "max_tokens": self.max_tokens,
            })
            self.usage.calls += 1
            usage = body.get("usage") or {}
            self.usage.prompt_tokens += usage.get("prompt_tokens", 0)
            self.usage.completion_tokens += usage.get("completion_tokens", 0)

            choices = body.get("choices") or []
            if not choices:
                break
            message = choices[0].get("message") or {}
            calls = message.get("tool_calls") or []

            if not calls:
                # The model replied in prose instead of calling the decision
                # tool. That is not a decision, so it is not treated as one.
                break

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            })

            for call in calls:
                function = call.get("function") or {}
                name = function.get("name", "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}

                if name == "submit_decision":
                    return self._finalise(arguments, toolbox, candidate_ids)

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": name,
                    "content": toolbox.call(name, arguments),
                })

        self.usage.failed += 1
        return AdjudicationResult(
            "abstain", (), 0.0,
            f"agent did not reach a decision within {self.max_turns} turns",
        )

    def _finalise(
        self, arguments: dict[str, Any], toolbox: Toolbox, candidate_ids: list[str]
    ) -> AdjudicationResult:
        reasoning = str(arguments.get("reasoning", "")).strip()
        investigation = tuple(
            Evidence(
                kind="agent_tool_call",
                detail=f"{call.summary()} -> {call.result[:220]}",
                weight=0.0,
                record_ids=(toolbox.settlement_id,),
            )
            for call in toolbox.calls
        )

        if arguments.get("decision") != "match":
            self.usage.declined += 1
            return AdjudicationResult(
                "abstain", (), 0.0,
                reasoning or "agent declined to decide",
                evidence=investigation,
            )

        chosen = str(arguments.get("line_id") or "")
        if chosen not in candidate_ids:
            # Naming a record it was never offered is a fabrication. Discarded
            # rather than resolved -- the verifier would catch it downstream,
            # but it should never get that far.
            self.usage.failed += 1
            return AdjudicationResult(
                "abstain", (), 0.0,
                f"agent returned id {chosen!r}, which was not among the candidates "
                f"offered; the proposal was discarded",
                evidence=investigation,
            )

        try:
            confidence = max(0.0, min(1.0, float(arguments.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0

        self.usage.decided += 1
        return AdjudicationResult(
            decision="match",
            chosen_ids=(chosen,),
            confidence=confidence,
            rationale=reasoning or "agent matched without stating a reason",
            evidence=investigation + (
                Evidence(
                    kind="agent_conclusion",
                    detail=reasoning[:500] or "no reasoning supplied",
                    weight=confidence,
                    record_ids=(toolbox.settlement_id, chosen),
                ),
            ),
        )
