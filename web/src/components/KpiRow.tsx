import { cx, num, pct } from '../lib/format'
import type { RunSummary } from '../types'
import { Skeleton } from './primitives'

type Tone = 'ok' | 'warn' | 'crit' | 'plain'

const ACCENT: Record<Tone, string> = {
  ok: 'bg-ok',
  warn: 'bg-warn',
  crit: 'bg-crit',
  plain: 'bg-accent',
}

const VALUE: Record<Tone, string> = {
  ok: 'text-ok',
  warn: 'text-warn',
  crit: 'text-crit',
  plain: '',
}

export function KpiSkeleton() {
  return (
    <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
      {Array.from({ length: 5 }, (_, i) => (
        <Skeleton key={i} className="h-[88px] rounded-[14px]" />
      ))}
    </div>
  )
}

export function KpiRow({ run }: { run: RunSummary }) {
  const card = run.scorecard
  const falseRate = card ? card.false_match_rate : 0

  const items: Array<{ label: string; value: string; sub: string; tone: Tone }> = [
    {
      label: 'throughput',
      value: num(Math.round(run.throughput_per_second)) + '/s',
      sub: num(run.records) + ' records in ' + run.seconds.toFixed(2) + 's',
      tone: 'plain',
    },
    {
      label: 'match rate',
      value: pct(run.match_rate),
      sub: num(run.matches) + ' matches · ' + run.value_matched_display,
      tone: 'ok',
    },
    {
      label: 'false match rate',
      value: card ? pct(falseRate, 2) : '—',
      sub: falseRate === 0 ? 'no wrong answer reached the output' : 'review immediately',
      tone: falseRate === 0 ? 'ok' : 'crit',
    },
    {
      label: 'open exceptions',
      value: num(run.exceptions),
      sub: run.value_at_risk_display + ' at high severity',
      tone: 'warn',
    },
    {
      label: 'ledger',
      value: run.journal_balances ? 'balanced' : 'out',
      sub: num(run.journal_entries) + ' entries posted',
      tone: run.journal_balances ? 'ok' : 'crit',
    },
  ]

  return (
    <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
      {items.map((k) => (
        <div
          key={k.label}
          className="relative overflow-hidden rounded-[14px] border border-line bg-surface px-4 py-3.5"
        >
          <span
            aria-hidden
            className={cx('absolute inset-y-0 left-0 w-[3px] opacity-85', ACCENT[k.tone])}
          />
          <div className="mb-1 text-[10.5px] font-semibold uppercase tracking-[0.085em] text-ink-mut">
            {k.label}
          </div>
          <div
            className={cx(
              'font-mono text-[21px] font-bold leading-tight tracking-tight sm:text-[27px]',
              VALUE[k.tone],
            )}
          >
            {k.value}
          </div>
          <div className="mt-0.5 text-[10.5px] text-ink-mut sm:text-[11.5px]">{k.sub}</div>
        </div>
      ))}
    </div>
  )
}
