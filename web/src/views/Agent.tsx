import { useState } from 'react'

import { cx, num, titleise } from '../lib/format'
import type { AgentCase, AgentPayload } from '../types'
import { Card, Note } from '../components/primitives'

/* ── how the agent sits in the system ─────────────────────────────────
   The single most important thing to convey is that the agent proposes
   and the verifier disposes. Stated in prose it reads as a claim; drawn
   as a flow with the veto on it, it reads as a mechanism.            */

function TrustBoundary() {
  return (
    <figure className="m-0">
      <div className="overflow-x-auto rounded-none border border-line bg-surface-1 p-4">
        <svg
          viewBox="0 0 720 132"
          role="img"
          aria-label="The deterministic cascade resolves most records and passes only its residual to the agent. The agent investigates with tools and proposes a decision. A verifier recomputes the arithmetic from the original records and either posts the match to the journal or vetoes it onto the exception register."
          className="block h-auto w-full min-w-[560px]"
        >
          <defs>
            <marker id="ag-a" viewBox="0 0 10 10" refX="9" refY="5"
                    markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 1 L9 5 L0 9 z" fill="currentColor" fillOpacity=".45" />
            </marker>
            <marker id="ag-v" viewBox="0 0 10 10" refX="9" refY="5"
                    markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 1 L9 5 L0 9 z" fill="var(--crit)" />
            </marker>
          </defs>

          <rect x="4" y="34" width="134" height="46" rx="4"
                className="fill-surface-2 stroke-current opacity-90" strokeOpacity=".25" />
          <text x="71" y="54" textAnchor="middle" className="fill-ink text-[11.5px] font-medium">Cascade</text>
          <text x="71" y="69" textAnchor="middle" className="fill-ink-mut font-mono text-[8.5px]">88.7% resolved here</text>

          <path d="M138 57 H186" className="fill-none stroke-current" strokeOpacity=".4" markerEnd="url(#ag-a)" />
          <text x="162" y="49" textAnchor="middle" className="fill-ink-mut font-mono text-[8.5px]">residual</text>

          <rect x="188" y="30" width="156" height="54" rx="4"
                fill="var(--accent-soft)" stroke="var(--accent)" strokeOpacity=".8" />
          <text x="266" y="50" textAnchor="middle" className="fill-ink text-[11.5px] font-medium">Agent</text>
          <text x="266" y="64" textAnchor="middle" className="fill-ink-mut font-mono text-[8.5px]">investigates with tools</text>
          <text x="266" y="76" textAnchor="middle" className="fill-ink-mut font-mono text-[8.5px]">may decline</text>

          <path d="M344 57 H392" className="fill-none stroke-current" strokeOpacity=".4" markerEnd="url(#ag-a)" />
          <text x="368" y="49" textAnchor="middle" className="fill-ink-mut font-mono text-[8.5px]">proposes</text>

          <rect x="394" y="30" width="150" height="54" rx="4"
                className="fill-surface-2 stroke-current" strokeOpacity=".25" />
          <text x="469" y="50" textAnchor="middle" className="fill-ink text-[11.5px] font-medium">Verifier</text>
          <text x="469" y="64" textAnchor="middle" className="fill-ink-mut font-mono text-[8.5px]">recomputes from source</text>
          <text x="469" y="76" textAnchor="middle" className="fill-ink-mut font-mono text-[8.5px]">holds the veto</text>

          <path d="M544 44 H600 V26" className="fill-none stroke-current" strokeOpacity=".4" markerEnd="url(#ag-a)" />
          <text x="606" y="20" className="fill-ok font-mono text-[8.5px]">accepted &rarr; journal</text>

          <path d="M544 70 H600 V108" fill="none" stroke="var(--crit)" strokeWidth="1.4" markerEnd="url(#ag-v)" />
          <text x="606" y="114" className="font-mono text-[8.5px]" fill="var(--crit)">vetoed &rarr; register</text>
        </svg>
      </div>
      <figcaption className="mt-2 max-w-2xl text-[12px] leading-snug text-ink-mut">
        The agent sits outside the trust boundary. It never writes to the ledger and
        never has the last word &mdash; whatever it concludes is recomputed from the
        original records before anything is posted.
      </figcaption>
    </figure>
  )
}

/* ── one recorded investigation ───────────────────────────────────── */

function CaseCard({ item, index }: { item: AgentCase; index: number }) {
  const [open, setOpen] = useState(index === 0)
  const declined = item.decision === 'decline'
  const vetoed = item.verifier?.ran === true && item.verifier.accepted === false
  const posted = item.verifier?.ran === true && item.verifier.accepted === true

  const badge = vetoed ? 'vetoed' : declined ? 'declined' : posted ? 'matched' : 'proposed'
  const badgeTone = vetoed
    ? 'bg-crit-soft text-crit'
    : declined
      ? 'bg-warn-soft text-warn'
      : 'bg-ok-soft text-ok'

  return (
    <article
      className={cx(
        'overflow-hidden rounded-none border bg-surface-1 transition-colors',
        vetoed ? 'border-crit/45' : declined ? 'border-line' : 'border-ok/40',
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-start gap-3 p-4 text-left hover:bg-surface-2"
      >
        <span
          className={cx(
            'mt-[3px] shrink-0 rounded-none px-1.5 py-[2px] font-mono text-[10px] font-bold uppercase tracking-wider',
            badgeTone,
          )}
        >
          {badge}
        </span>

        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
            <span className="font-mono text-[13px] font-medium">{item.payout_amount}</span>
            <span className="font-mono text-[11px] text-ink-mut">{item.settlement_id}</span>
            <span className="font-mono text-[11px] text-accent">{titleise(item.reason)}</span>
          </span>
          <span className="mt-1 block text-[12.5px] leading-snug text-ink-2">
            {item.reason_in_english}
          </span>
          <span className="mt-1.5 block font-mono text-[11px] text-ink-mut">
            {item.steps.length} tool call{item.steps.length === 1 ? '' : 's'} ·{' '}
            {item.candidate_count} candidate{item.candidate_count === 1 ? '' : 's'} ·{' '}
            reference {item.reference ?? 'none'}
          </span>
        </span>

        <span className="mt-1 shrink-0 font-mono text-[11px] text-ink-mut">
          {open ? '−' : '+'}
        </span>
      </button>

      {open && (
        <div className="border-t border-line-soft px-4 pb-4 pt-3">
          <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.09em] text-ink-mut">
            What it chose to look at
          </p>

          <ol className="m-0 flex list-none flex-col gap-2 p-0">
            {item.steps.map((step, i) => (
              <li key={i} className="rounded-none bg-surface-2 p-2.5">
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-[10px] font-bold text-accent">
                    {String(i + 1).padStart(2, '0')}
                  </span>
                  <span className="break-all font-mono text-[11.5px] text-ink">
                    {step.tool}(
                    {Object.entries(step.arguments)
                      .map(([k, v]) => `${k}=${String(v)}`)
                      .join(', ')}
                    )
                  </span>
                </div>
                <pre className="mt-1.5 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded-none bg-surface-1 p-2 font-mono text-[10.5px] leading-relaxed text-ink-mut">
                  {step.result}
                </pre>
              </li>
            ))}
            {item.steps.length === 0 && (
              <li className="text-[12px] text-ink-mut">
                No tools called &mdash; the summary alone was enough to decline.
              </li>
            )}
          </ol>

          <p className="mb-1.5 mt-4 font-mono text-[10px] uppercase tracking-[0.09em] text-ink-mut">
            What it concluded
          </p>
          <p
            className={cx(
              'rounded-none border-l-2 bg-surface-2 p-3 text-[12.5px] leading-relaxed text-ink-2',
              vetoed ? 'border-crit' : declined ? 'border-warn' : 'border-ok',
            )}
          >
            {item.reasoning}
          </p>

          {item.verifier?.ran && (
            <>
              <p className="mb-1.5 mt-4 font-mono text-[10px] uppercase tracking-[0.09em] text-ink-mut">
                What the verifier did with it
              </p>
              {vetoed ? (
                <div className="rounded-none border border-crit/40 bg-crit-soft p-3">
                  <p className="text-[12.5px] font-semibold text-crit">
                    Proposal rejected. It never reached the ledger.
                  </p>
                  {item.verifier.violations?.map((v, i) => (
                    <p key={i} className="mt-1.5 font-mono text-[11px] leading-snug text-ink-2">
                      <span className="text-crit">{v.invariant}</span> &mdash; {v.detail}
                    </p>
                  ))}
                  <p className="mt-2 text-[12px] leading-snug text-ink-mut">
                    The agent was articulate and confident, and the arithmetic disagreed.
                    Arithmetic wins.
                  </p>
                </div>
              ) : (
                <p className="rounded-none border border-ok/35 bg-ok-soft p-3 text-[12.5px] text-ok">
                  Recomputed from the original records and accepted. Posted to the journal.
                </p>
              )}
            </>
          )}
        </div>
      )}
    </article>
  )
}

/* ── the view ─────────────────────────────────────────────────────── */

export function Agent({ data }: { data: AgentPayload }) {
  const u = data.usage ?? {}
  // Every case the agent was handed, including the ones where it failed to
  // conclude. Counting only the decisions would quietly drop those, and a
  // failure to decide is part of the record.
  const investigated = data.cases.length

  return (
    <div className="flex flex-col gap-5">
      <Card
        span={12}
        title="The agent"
        blurb="When the deterministic cascade cannot explain a payout, an LLM takes the case. It is given tools, not answers, and decides for itself what to investigate."
      >
        <TrustBoundary />
      </Card>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
        <Card span={5} title="What it is allowed to do" blurb="Seven read-only tools — the same primitives the deterministic cascade uses.">
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {data.tools.map((tool) => (
              <li key={tool.name} className="border-b border-line-soft pb-2 last:border-b-0">
                <code className="font-mono text-[11.5px] font-medium text-accent">
                  {tool.name}
                </code>
                <p className="mt-0.5 text-[12px] leading-snug text-ink-mut">
                  {tool.description}
                </p>
              </li>
            ))}
          </ul>
        </Card>

        <Card span={7} title="What it actually did" blurb="Measured across the recorded cases below.">
          <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
            {[
              ['cases investigated', num(investigated)],
              ['tool calls', num(u.tool_calls ?? 0)],
              ['matched', num(u.decided ?? 0)],
              ['declined', num(u.declined ?? 0)],
              ['failed, degraded to abstention', num(u.failed ?? 0)],
              ['tokens in / out', `${num(u.prompt_tokens ?? 0)} / ${num(u.completion_tokens ?? 0)}`],
            ].map(([label, value]) => (
              <div key={label}>
                <div className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-mut">
                  {label}
                </div>
                <div className="mt-0.5 font-mono text-[17px] font-medium tabular-nums">
                  {value}
                </div>
              </div>
            ))}
          </div>

          <Note tone={data.mode === 'live' ? 'ok' : undefined}>
            {data.mode === 'recorded' && (
              <>
                <strong>Recorded from a real run</strong> against{' '}
                <code className="font-mono text-[11.5px]">{data.model}</code> on{' '}
                {data.recorded_at?.slice(0, 10)}. Every tool call and result below is
                what the model actually produced &mdash; nothing is simulated.{' '}
              </>
            )}
            {data.mode === 'live' && (
              <>
                <strong>Live run</strong> against{' '}
                <code className="font-mono text-[11.5px]">{data.model}</code>.{' '}
              </>
            )}
            {data.note}
          </Note>
        </Card>
      </div>

      <Card
        span={12}
        title={`Investigations (${data.cases.length})`}
        blurb="Every case the deterministic passes gave up on. Expand one to see the exact tools the agent chose, what came back, and the conclusion it reached."
      >
        {data.cases.length ? (
          <div className="flex flex-col gap-2.5">
            {data.cases.map((item, i) => (
              <CaseCard key={item.settlement_id} item={item} index={i} />
            ))}
          </div>
        ) : (
          <p className="text-[12.5px] text-ink-mut">{data.note}</p>
        )}
      </Card>
    </div>
  )
}
