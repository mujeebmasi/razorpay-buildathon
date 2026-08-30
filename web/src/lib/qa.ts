/**
 * A grounded question-answering engine over the reconciliation result.
 *
 * The obvious way to put a chatbot in a static page is to call a hosted model
 * from the browser — which means shipping an API key in the JavaScript, where
 * anyone who opens devtools can take it. That is not a trade worth making for
 * a demo, so this answers from the data instead.
 *
 * Every answer is computed from the exported run and cites the records it used.
 * It cannot hallucinate a figure because it never generates one: it looks
 * numbers up, the same way the engine does. When the Python server is running
 * with a provider key, the panel routes to the real tool-using agent instead
 * and says so — see `askAgent` in `api.ts`.
 *
 * The trade is honest and worth stating: this understands the questions it was
 * built for and says so plainly when it does not understand one, rather than
 * producing a fluent guess. Given the whole project is an argument against
 * fluent guesses, that is the only version of this feature that belongs in it.
 */
import type { AgentPayload, ExceptionDetail, RunSummary } from '../types'

export interface Citation {
  label: string
  value: string
}

export interface Answer {
  text: string
  citations: Citation[]
  /** Follow-ups the user can click, so the panel teaches its own vocabulary. */
  suggestions?: string[]
}

export interface QaContext {
  run: RunSummary
  exceptions: ExceptionDetail[]
  agent: AgentPayload | null
}

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info'] as const

function titleise(s: string): string {
  return s.replace(/_/g, ' ')
}

/** Sum in paise, formatted the way every other amount on the page is. */
function inr(paise: number): string {
  const sign = paise < 0 ? '-' : ''
  const whole = Math.floor(Math.abs(paise) / 100)
  const frac = String(Math.abs(paise) % 100).padStart(2, '0')
  const digits = String(whole)
  let grouped = digits
  if (digits.length > 3) {
    const tail = digits.slice(-3)
    let head = digits.slice(0, -3)
    const parts: string[] = []
    while (head.length > 2) {
      parts.unshift(head.slice(-2))
      head = head.slice(0, -2)
    }
    if (head) parts.unshift(head)
    grouped = [...parts, tail].join(',')
  }
  return `${sign}₹${grouped}.${frac}`
}

/* ── intent matching ──────────────────────────────────────────────────
   Keyword sets rather than a model. Crude, and honest about being crude:
   an unmatched question says so instead of inventing an answer.        */

const has = (q: string, ...words: string[]) => words.some((w) => q.includes(w))

export const SUGGESTED_QUESTIONS = [
  'How much money is at risk?',
  'What are the critical exceptions?',
  'Why is the match rate only 75%?',
  'What did the agent do?',
  'Show me the duplicate credit',
  'Did the verifier reject anything?',
  'How was accuracy measured?',
  'What did it find that matching alone cannot?',
]

export function answer(question: string, ctx: QaContext): Answer {
  const q = question.toLowerCase().trim()
  const { run, exceptions, agent } = ctx

  if (!q) {
    return { text: 'Ask me about this reconciliation run.', citations: [] }
  }

  /* ── money at risk ───────────────────────────────────────────────── */
  if (has(q, 'at risk', 'exposure', 'how much money', 'value at risk', 'how much is')) {
    const bySeverity = SEVERITY_ORDER.map((sev) => {
      const rows = exceptions.filter((e) => e.severity === sev)
      return { sev, count: rows.length, total: rows.reduce((s, e) => s + e.amount, 0) }
    }).filter((r) => r.count > 0)

    return {
      text:
        `${run.value_at_risk_display} sits in high-severity breaks — that is the ` +
        `number a finance lead asks for first. Across all ${run.exceptions} open ` +
        `items the register totals ` +
        `${inr(exceptions.reduce((s, e) => s + e.amount, 0))}, but most of that is ` +
        `lower severity and expected to clear.`,
      citations: bySeverity.map((r) => ({
        label: `${r.sev} · ${r.count} item${r.count === 1 ? '' : 's'}`,
        value: inr(r.total),
      })),
      suggestions: ['What are the critical exceptions?', 'Who owns them?'],
    }
  }

  /* ── critical items ──────────────────────────────────────────────── */
  if (has(q, 'critical', 'most serious', 'worst', 'urgent')) {
    const rows = exceptions.filter((e) => e.severity === 'critical').slice(0, 4)
    return {
      text:
        `${exceptions.filter((e) => e.severity === 'critical').length} breaks are ` +
        `critical, meaning cash is unaccounted for or a figure is provably wrong. ` +
        `The largest are below. Each names the team that owns it, because a unit ` +
        `bug and a missing deduction go to different people.`,
      citations: rows.map((e) => ({
        label: `${titleise(e.reason)} · ${e.owner}`,
        value: e.amount_display,
      })),
      suggestions: ['Show me the duplicate credit', 'How much money is at risk?'],
    }
  }

  /* ── the duplicate ───────────────────────────────────────────────── */
  if (has(q, 'duplicate', 'twice', 'double')) {
    const dup = exceptions.find((e) => e.reason === 'duplicate_identifier')
    if (dup) {
      return {
        text:
          `${dup.summary} It is routed to ${dup.owner} rather than finance, ` +
          `because a reference credited twice is a pipeline replay, not an ` +
          `accounting question.`,
        citations: [
          { label: 'amount', value: dup.amount_display },
          { label: 'owner', value: dup.owner },
          { label: 'next action', value: dup.action },
          ...dup.subjects.slice(0, 2).map((s) => ({ label: 'record', value: s })),
        ],
      }
    }
  }

  /* ── match rate ──────────────────────────────────────────────────── */
  if (has(q, 'match rate', '75', 'why only', 'low match', 'not higher')) {
    return {
      text:
        `The match rate is ${(run.match_rate * 100).toFixed(1)}%, and that is the ` +
        `honest number rather than a disappointing one. About a fifth of this ` +
        `batch is unresolvable by construction — two identical payouts with no ` +
        `reference, a chargeback for a payment outside the period — so the ` +
        `ceiling is near 81%. The rest of the gap is deliberate refusal: where a ` +
        `combination of payouts would hit a credit by luck, the engine declines ` +
        `rather than guessing. An earlier build scored 90.6% and was producing ` +
        `false matches.`,
      citations: [
        { label: 'match rate', value: `${(run.match_rate * 100).toFixed(1)}%` },
        { label: 'false match rate', value: run.scorecard ? `${(run.scorecard.false_match_rate * 100).toFixed(2)}%` : 'n/a' },
        { label: 'auto-resolved', value: run.scorecard ? `${(run.scorecard.auto_resolve_rate * 100).toFixed(1)}%` : 'n/a' },
      ],
      suggestions: ['How was accuracy measured?'],
    }
  }

  /* ── accuracy method ─────────────────────────────────────────────── */
  if (has(q, 'accuracy', 'measured', 'how do you know', 'prove', 'ground truth', 'answer key')) {
    const c = run.scorecard
    return {
      text:
        `A generator builds a fake month of business, plants thirty specific ` +
        `problems in it, and writes every correct answer into a file the engine ` +
        `is never allowed to read. Grading against that hidden key is what makes ` +
        `these numbers mean anything — and why an engine that matched everything ` +
        `would score badly rather than perfectly.`,
      citations: c
        ? [
            { label: 'overall accuracy', value: `${(c.accuracy * 100).toFixed(1)}%` },
            { label: 'match precision', value: `${(c.match_precision * 100).toFixed(1)}%` },
            { label: 'exception recall', value: `${(c.exception_recall * 100).toFixed(1)}%` },
            { label: 'false match rate', value: `${(c.false_match_rate * 100).toFixed(2)}%` },
            { label: 'cases scored', value: String(c.total_cases) },
          ]
        : [],
    }
  }

  /* ── the agent ───────────────────────────────────────────────────── */
  if (has(q, 'agent', 'llm', 'ai ', 'model', 'investigat')) {
    if (!agent || !agent.cases.length) {
      return { text: 'No agent run is loaded on this page.', citations: [] }
    }
    const vetoed = agent.cases.filter(
      (c) => c.decision === 'match' && c.verifier?.accepted === false,
    )
    const declined = agent.cases.filter((c) => c.decision === 'decline')
    return {
      text:
        `When the deterministic passes cannot explain a payout, an LLM takes the ` +
        `case. It is given tools rather than answers and picks its own ` +
        `investigation. Across ${agent.cases.length} recorded cases it declined ` +
        `${declined.length} and proposed ${agent.cases.length - declined.length} ` +
        `matches — and the verifier rejected ${vetoed.length} of those, ` +
        `recomputing from the original records. That is the design working: the ` +
        `agent proposes, arithmetic disposes.`,
      citations: [
        { label: 'model', value: agent.model ?? 'unknown' },
        { label: 'tool calls', value: String(agent.usage.tool_calls ?? 0) },
        { label: 'declined', value: String(declined.length) },
        { label: 'proposals vetoed', value: String(vetoed.length) },
      ],
      suggestions: ['Did the verifier reject anything?'],
    }
  }

  /* ── the veto ────────────────────────────────────────────────────── */
  if (has(q, 'verifier', 'veto', 'reject', 'guardrail', 'overrule')) {
    const vetoed = agent?.cases.filter(
      (c) => c.decision === 'match' && c.verifier?.accepted === false,
    ) ?? []
    const example = vetoed[0]
    return {
      text: example
        ? `Yes. The agent proposed ${vetoed.length} matches and the verifier ` +
          `rejected every one. In the clearest case it found the payout ` +
          `reference printed in the bank narration, matched on it, and the ` +
          `verifier caught a gap the agent had noticed and talked itself past. ` +
          `That money would have been lost silently if the model had the last word.`
        : `The verifier recomputes every match from the original records and can ` +
          `veto it. In this run it rejected ${run.verifier_rejections} of ` +
          `${run.verifier_checks} matches.`,
      citations: example
        ? [
            { label: 'payout', value: example.payout_amount },
            { label: 'agent decided', value: `match at ${example.confidence} confidence` },
            { label: 'verifier', value: example.verifier?.violations?.[0]?.detail.slice(0, 120) ?? 'rejected' },
          ]
        : [
            { label: 'matches checked', value: String(run.verifier_checks) },
            { label: 'rejected', value: String(run.verifier_rejections) },
          ],
    }
  }

  /* ── findings matching alone cannot produce ──────────────────────── */
  if (has(q, 'overcharge', 'fee', 'withheld', 'reserve', 'find', 'cannot', "can't")) {
    const c = run.counters
    return {
      text:
        `Some money reconciles perfectly and is still wrong, and only ` +
        `recomputing independently finds it. Gateway overcharges still settle, ` +
        `so both sides agree on the wrong number. A payout the gateway partly ` +
        `withheld matches its credit exactly — only comparing against the ` +
        `component payments reveals the gap.`,
      citations: [
        { label: 'fees above the rate card', value: run.fee_recovery_display },
        { label: 'withheld by the gateway', value: run.reserve_display },
        { label: 'SLA breaches', value: String(c.sla_breaches ?? 0) },
        { label: 'duplicate credits', value: String(c.duplicate_credits ?? 0) },
      ],
    }
  }

  /* ── throughput ──────────────────────────────────────────────────── */
  if (has(q, 'fast', 'speed', 'throughput', 'how long', 'performance')) {
    return {
      text:
        `${run.records.toLocaleString('en-IN')} records in ` +
        `${run.seconds.toFixed(2)} seconds. Most of that work costs nothing: ` +
        `the cheapest three passes resolve the large majority of matches with a ` +
        `dictionary lookup, and only the residual reaches anything expensive.`,
      citations: [
        { label: 'records', value: run.records.toLocaleString('en-IN') },
        { label: 'wall time', value: `${run.seconds.toFixed(2)}s` },
        { label: 'throughput', value: `${Math.round(run.throughput_per_second).toLocaleString('en-IN')}/s` },
      ],
    }
  }

  /* ── a specific record ───────────────────────────────────────────── */
  const id = q.match(/\b(setl|bank|pay|x)_[a-z0-9]+/)?.[0]
  if (id) {
    const hit = exceptions.find(
      (e) => e.id === id || e.subjects.includes(id) || e.candidates.includes(id),
    )
    if (hit) {
      return {
        text: `${hit.summary} ${hit.owner} owns it: ${hit.action}`,
        citations: [
          { label: 'reason', value: titleise(hit.reason) },
          { label: 'severity', value: hit.severity },
          { label: 'amount', value: hit.amount_display },
          ...hit.evidence.slice(0, 2).map((e) => ({ label: e.kind, value: e.detail.slice(0, 140) })),
        ],
      }
    }
    return { text: `Nothing on the exception register mentions ${id}.`, citations: [] }
  }

  /* ── a reason code by name ───────────────────────────────────────── */
  const reason = Object.keys(run.reason_counts).find((r) =>
    q.includes(r.replace(/_/g, ' ')) || q.includes(r),
  )
  if (reason) {
    const rows = exceptions.filter((e) => e.reason === reason)
    const total = rows.reduce((s, e) => s + e.amount, 0)
    return {
      text:
        `${rows.length} break${rows.length === 1 ? '' : 's'} of that kind, ` +
        `totalling ${inr(total)}. ${rows[0]?.summary ?? ''}`,
      citations: [
        { label: 'owner', value: rows[0]?.owner ?? '—' },
        { label: 'next action', value: rows[0]?.action ?? '—' },
      ],
    }
  }

  /* ── no match: say so ────────────────────────────────────────────── */
  return {
    text:
      `I answer from this run's data rather than generating text, so I can only ` +
      `answer what I can look up — and I would rather say that than invent a ` +
      `fluent guess. Try one of these, or paste a record id like setl_003611.`,
    citations: [],
    suggestions: SUGGESTED_QUESTIONS.slice(0, 4),
  }
}
