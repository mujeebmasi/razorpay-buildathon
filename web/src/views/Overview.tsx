import { SEVERITY_VAR } from '../lib/api'
import { cx, num, pct, titleise, TIER_NAMES } from '../lib/format'
import type { RunSummary } from '../types'
import { Bar, Card, EvidenceItem, Metric, Note } from '../components/primitives'
import { DailyChart } from '../components/DailyChart'

/* ── where the work went ──────────────────────────────────────────── */

function Funnel({ run }: { run: RunSummary }) {
  const tiers = Object.entries(run.tier_counts).sort(([a], [b]) => a.localeCompare(b))
  const total = tiers.reduce((sum, [, n]) => sum + (n ?? 0), 0) || 1
  const max = Math.max(...tiers.map(([, n]) => n ?? 0), 1)

  // The label sits above the bar rather than inside it. Overlaying text on a
  // partially-filled bar means it crosses two different backgrounds, so one of
  // the two themes always ends up with unreadable contrast somewhere along it.
  return (
    <div className="flex flex-col gap-3">
      {tiers.map(([tier, count]) => (
        <div key={tier}>
          <div className="mb-1.5 flex items-baseline gap-2.5">
            <span className="shrink-0 rounded bg-accent-soft px-1.5 py-[2px] font-mono text-[10.5px] font-semibold text-accent">
              {tier}
            </span>
            <span className="min-w-0 flex-1 truncate text-[12px] text-ink-2">
              {TIER_NAMES[tier] ?? tier}
            </span>
            <span className="shrink-0 font-mono text-[12.5px]">{num(count)}</span>
            <span className="w-11 shrink-0 text-right font-mono text-[11.5px] text-ink-mut">
              {pct((count ?? 0) / total)}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-surface-2">
            <div
              className="h-full rounded-full bg-gradient-to-r from-accent-2 to-accent transition-[width] duration-700 ease-[cubic-bezier(.22,.8,.28,1)]"
              style={{ width: pct((count ?? 0) / max) }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

/* ── accuracy ─────────────────────────────────────────────────────── */

function Accuracy({ run }: { run: RunSummary }) {
  const card = run.scorecard
  if (!card) {
    return <p className="text-[12.5px] text-ink-mut">No labels present, so accuracy cannot be measured.</p>
  }

  return (
    <>
      <Metric head label="overall accuracy" value={pct(card.accuracy)} tone="ok" />
      <Metric label="match precision" value={pct(card.match_precision)} tone="ok" />
      <Metric label="match recall" value={pct(card.match_recall)} />
      <Metric label="exception recall" value={pct(card.exception_recall)} tone="ok" />
      <Metric label="reason-code accuracy" value={pct(card.reason_accuracy)} tone="ok" />
      <Metric label="auto-resolved" value={pct(card.auto_resolve_rate)} />
      <Metric label="cases scored" value={num(card.total_cases)} />
      <Note>
        <strong className="text-ok">{pct(card.false_match_rate, 2)} false match rate.</strong>{' '}
        The engine never claimed a match it could not prove. About a fifth of this batch is
        unresolvable by construction, so an auto-resolve rate near {pct(card.auto_resolve_rate)} is
        the ceiling &mdash; not a shortfall.
      </Note>
    </>
  )
}

/* ── the guardrail ────────────────────────────────────────────────── */

function Guardrail({ run }: { run: RunSummary }) {
  const rejected = run.verifier_rejections
  const fromAdjudicator = run.verifier_rejected_adjudications

  return (
    <>
      <Metric head label="matches re-derived from source" value={num(run.verifier_checks)} tone="ok" />
      <Metric
        label="rejected for failing an invariant"
        value={num(rejected)}
        tone={rejected ? 'warn' : 'ok'}
      />
      <Metric
        label="of those, reasoning-layer proposals"
        value={num(fromAdjudicator)}
        tone={fromAdjudicator ? 'warn' : 'ok'}
      />
      <Metric label="adjudicator abstentions" value={num(run.adjudicator_abstained)} />

      {run.rejected_examples.slice(0, 3).map((v) => (
        <EvidenceItem
          key={v.match + v.invariant}
          kind={v.invariant + (v.adjudicated ? ' · adjudicator proposal' : '')}
          detail={v.detail}
          weight={-1}
        />
      ))}

      <Note tone={rejected ? 'warn' : 'ok'}>
        The verifier recomputes every total from the original records rather than trusting what a
        match says about itself, so a confident but wrong proposal cannot reach the ledger.
      </Note>
    </>
  )
}

/* ── exposure ─────────────────────────────────────────────────────── */

function Exposure({
  run,
  onPick,
}: {
  run: RunSummary
  onPick: (reason: string) => void
}) {
  const rows = run.exposure_by_reason.slice(0, 8)
  if (!rows.length) return null
  const max = Math.max(...rows.map((r) => r.amount), 1)

  return (
    <div>
      {rows.map((row) => (
        <button
          key={row.reason}
          type="button"
          onClick={() => onPick(row.reason)}
          className="group grid w-full grid-cols-[1fr_82px] items-center gap-3 border-b border-line-soft py-2 text-left last:border-b-0 sm:grid-cols-[1fr_96px]"
        >
          <div>
            <div className="text-[12.5px] transition-colors group-hover:text-accent">
              {titleise(row.reason)}
              <small className="font-mono text-[11px] text-ink-mut">
                {' '}
                · {num(row.count)} item{row.count === 1 ? '' : 's'}
              </small>
            </div>
            <Bar
              className="mt-1.5"
              fraction={row.amount / max}
              colour={SEVERITY_VAR[row.severity]}
            />
          </div>
          <span className="whitespace-nowrap text-right font-mono text-xs">
            {row.amount_display}
          </span>
        </button>
      ))}
    </div>
  )
}

/* ── findings ─────────────────────────────────────────────────────── */

function Findings({ run }: { run: RunSummary }) {
  const c = run.counters
  const rows: Array<[string, string, string]> = [
    [
      'gateway fees above the rate card',
      run.fee_recovery_display,
      'recoverable money a match-only reconciler never sees',
    ],
    [
      'payouts breaching the settlement SLA',
      num(c.sla_breaches ?? 0),
      'reference and amount agree, but the money arrived late',
    ],
    [
      'duplicate credits caught',
      num(c.duplicate_credits ?? 0),
      'the same reference credited more than once',
    ],
    [
      'reversal pairs netted',
      num(c.reversals_netted ?? 0),
      'absorbed silently instead of becoming two breaks',
    ],
    [
      'batched credits decomposed',
      num(c.batches_decomposed ?? 0),
      'one transfer traced back to its component payouts',
    ],
    [
      'withheld by the gateway',
      run.reserve_display,
      'posted as a receivable, not written off',
    ],
  ]

  return (
    <>
      {rows.map(([label, value, note]) => (
        <Metric key={label} label={label} note={note} value={value} />
      ))}
    </>
  )
}

/* ── the agent, when one ran ──────────────────────────────────────── */

function Agent({ run }: { run: RunSummary }) {
  const agent = run.agent
  if (!agent) return null

  const investigated = agent.decided + agent.declined
  const perCase = investigated ? agent.tool_calls / investigated : 0

  return (
    <Card
      span={5}
      title="The agent"
      blurb="Investigates what the deterministic cascade could not explain, using the engine's own instruments."
    >
      <Metric head label="cases investigated" value={num(investigated)} />
      <Metric
        label="tool calls made"
        note={perCase ? `${perCase.toFixed(1)} per case, chosen by the agent` : undefined}
        value={num(agent.tool_calls)}
      />
      <Metric label="matched" value={num(agent.decided)} tone="ok" />
      <Metric
        label="declined"
        note="refusing is a valid answer, not a failure"
        value={num(agent.declined)}
      />
      {agent.failed > 0 && (
        <Metric
          label="failed, degraded to abstention"
          value={num(agent.failed)}
          tone="warn"
        />
      )}
      <Metric
        label="tokens"
        note={`${num(agent.requests)} requests in ${agent.seconds.toFixed(1)}s`}
        value={`${num(agent.prompt_tokens)} / ${num(agent.completion_tokens)}`}
      />
      <Note>
        Everything the agent proposes still goes to the verifier, which recomputes from
        the original records and can veto it. A failure of any kind &mdash; transport,
        timeout, a fabricated id &mdash; becomes an abstention, never a match.
      </Note>
    </Card>
  )
}

/* ── the view ─────────────────────────────────────────────────────── */

export function Overview({
  run,
  onPickReason,
}: {
  run: RunSummary
  onPickReason: (reason: string) => void
}) {
  return (
    <div className={cx('grid grid-cols-12 gap-3.5')}>
      <Card
        span={8}
        title="Where the work went"
        blurb="Each pass sees only what the one before it could not explain. The cheap, certain tiers carry the volume."
      >
        <Funnel run={run} />
      </Card>

      <Card span={4} title="Accuracy" blurb="Against held-out labels the engine never sees.">
        <Accuracy run={run} />
      </Card>

      <Card
        span={7}
        title="Reconciled value by settlement date"
        blurb="Breaks cluster on particular days — a feed that lagged, a batch that would not decompose."
        aside={
          <div className="flex shrink-0 gap-3">
            <span className="inline-flex items-center gap-1.5 text-[11px] text-ink-mut">
              <i className="inline-block size-[9px] rounded-sm bg-ok" />
              matched
            </span>
            <span className="inline-flex items-center gap-1.5 text-[11px] text-ink-mut">
              <i className="inline-block size-[9px] rounded-sm bg-crit" />
              open
            </span>
          </div>
        }
      >
        <DailyChart data={run.daily} />
      </Card>

      <Card
        span={5}
        title="The guardrail"
        blurb="Every match is re-derived from source records by a verifier that can overrule the reasoning layer."
      >
        <Guardrail run={run} />
      </Card>

      <Agent run={run} />

      <Card
        span={7}
        title="Exposure by reason"
        blurb="Ranked by money, not by count — a large double-post is not one unit of the same thing as a rounding query."
      >
        <Exposure run={run} onPick={onPickReason} />
      </Card>

      <Card
        span={5}
        title="Findings a matcher alone would miss"
        blurb="Money that reconciles perfectly and is still wrong."
      >
        <Findings run={run} />
      </Card>
    </div>
  )
}
