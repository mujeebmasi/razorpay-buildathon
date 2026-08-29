import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { num, TIER_NAMES, titleise } from '../lib/format'
import { Empty, EvidenceItem, SkeletonRows, TierPill } from '../components/primitives'
import type { MatchPage } from '../types'

export function Matches({ tiers }: { tiers: string[] }) {
  const [tier, setTier] = useState('all')
  const [page, setPage] = useState<MatchPage | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    api
      .matches(tier, controller.signal)
      .then(setPage)
      .catch(() => undefined)
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [tier])

  return (
    <div>
      <div className="no-print mb-3 flex flex-wrap items-center gap-2.5">
        <select
          aria-label="Tier"
          value={tier}
          onChange={(e) => setTier(e.target.value)}
          className="select cursor-pointer rounded-[9px] border border-line bg-surface py-2.5 pl-3 pr-8 text-[12.5px] text-ink-2 transition-colors hover:border-accent focus:border-accent focus:outline-none"
        >
          <option value="all">All tiers</option>
          {tiers.map((t) => (
            <option key={t} value={t}>
              {t} &mdash; {TIER_NAMES[t] ?? t}
            </option>
          ))}
        </select>
        <p className="m-0 font-mono text-xs text-ink-mut" aria-live="polite">
          {page ? num(page.total) + ' matches' : 'loading…'}
        </p>
      </div>

      {loading && !page ? (
        <SkeletonRows count={5} />
      ) : !page || page.items.length === 0 ? (
        <Empty>No matches in this tier.</Empty>
      ) : (
        <div className="flex flex-col gap-[7px]">
          {page.items.map((m) => (
            <article
              key={m.id}
              className="rounded-[10px] border border-l-[3px] border-line border-l-ok bg-surface px-3.5 py-3"
            >
              <div className="flex items-baseline justify-between gap-3">
                <span className="flex min-w-0 flex-wrap items-center gap-2">
                  <TierPill tier={m.tier} />
                  <span className="font-mono text-[11.5px] text-accent">{m.reason}</span>
                </span>
                <span className="shrink-0 whitespace-nowrap font-mono text-[13px]">
                  {m.bank_total_display}
                </span>
              </div>

              <p className="m-0 mt-1.5 text-[12.5px] leading-snug text-ink-2">
                {m.rationale || TIER_NAMES[m.tier] || titleise(m.reason)}
              </p>

              <div className="mt-1.5 flex flex-wrap gap-2 font-mono text-[11px] text-ink-mut">
                <span>
                  {num(m.settlements.length)} payout(s) &rarr; {num(m.bank_lines.length)} credit(s)
                </span>
                <span>confidence {m.confidence}</span>
                <span>residual {m.residual} paise</span>
                {m.adjudicator ? <span>via {m.adjudicator}</span> : null}
              </div>

              {(m.evidence ?? []).slice(0, 2).map((e, i) => (
                <div key={i} className="mt-2">
                  <EvidenceItem kind={e.kind} detail={e.detail} weight={e.weight} />
                </div>
              ))}
            </article>
          ))}
        </div>
      )}
    </div>
  )
}
