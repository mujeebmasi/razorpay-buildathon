import type { ReactNode } from 'react'
import { cx } from '../lib/format'
import type { Severity } from '../types'

/* ── Card ─────────────────────────────────────────────────────────── */

export function Card({
  title, blurb, aside, span = 12, children,
}: {
  title?: string
  blurb?: ReactNode
  aside?: ReactNode
  /** Columns out of 12 on desktop; collapses to full width below `lg`. */
  span?: 4 | 5 | 6 | 7 | 8 | 12
  children: ReactNode
}) {
  const spans: Record<number, string> = {
    4: 'lg:col-span-6 xl:col-span-4',
    5: 'lg:col-span-6 xl:col-span-5',
    6: 'lg:col-span-6',
    7: 'lg:col-span-6 xl:col-span-7',
    8: 'lg:col-span-6 xl:col-span-8',
    12: 'lg:col-span-12',
  }
  return (
    <section className={cx('card col-span-12', spans[span])}>
      {(title || aside) && (
        <header className="mb-4 flex items-start justify-between gap-3.5">
          <div>
            {title && <h2 className="text-[13.5px] font-semibold tracking-tight">{title}</h2>}
            {blurb && (
              <p className="mt-0.5 max-w-[62ch] text-xs leading-relaxed text-ink-mut">{blurb}</p>
            )}
          </div>
          {aside}
        </header>
      )}
      {children}
    </section>
  )
}

/* ── Pill ─────────────────────────────────────────────────────────── */

const SEVERITY_PILL: Record<Severity, string> = {
  critical: 'bg-crit-soft text-crit',
  high: 'bg-high-soft text-high',
  medium: 'bg-warn-soft text-warn',
  low: 'bg-surface-3 text-ink-mut',
  info: 'bg-surface-3 text-ink-mut',
}

export function SeverityPill({ severity }: { severity: Severity }) {
  return (
    <span
      className={cx(
        'shrink-0 rounded-none px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider',
        SEVERITY_PILL[severity],
      )}
    >
      {severity}
    </span>
  )
}

export function TierPill({ tier }: { tier: string }) {
  return (
    <span className="shrink-0 rounded-none bg-accent-soft px-2 py-0.5 font-mono text-[10px] font-bold text-accent">
      {tier}
    </span>
  )
}

/* ── Metric row ───────────────────────────────────────────────────── */

export function Metric({
  label, note, value, tone, head = false,
}: {
  label: string
  note?: string
  value: ReactNode
  tone?: 'ok' | 'warn' | 'crit'
  head?: boolean
}) {
  const toneClass =
    tone === 'ok' ? 'text-ok' : tone === 'warn' ? 'text-warn' : tone === 'crit' ? 'text-crit' : ''
  return (
    <div
      className={cx(
        'flex items-baseline justify-between gap-3.5 border-b border-line-soft last:border-b-0',
        head ? 'py-2.5' : 'py-2',
      )}
    >
      <span className="text-[12.5px] text-ink-2">
        {label}
        {note && <small className="block text-[11px] leading-snug text-ink-mut">{note}</small>}
      </span>
      <span
        className={cx(
          'whitespace-nowrap font-mono',
          head ? 'text-xl font-bold tracking-tight' : 'text-[13.5px]',
          toneClass,
        )}
      >
        {value}
      </span>
    </div>
  )
}

/* ── Note ─────────────────────────────────────────────────────────── */

export function Note({
  tone = 'ok', children,
}: {
  tone?: 'ok' | 'warn'
  children: ReactNode
}) {
  return (
    <div
      className={cx(
        'mt-3.5 rounded-none border p-3 text-xs leading-relaxed text-ink-2',
        tone === 'ok'
          ? 'border-ok/30 bg-ok-soft'
          : 'border-warn/30 bg-warn-soft',
      )}
    >
      {children}
    </div>
  )
}

/* ── Evidence ─────────────────────────────────────────────────────── */

export function EvidenceItem({
  kind, detail, weight,
}: {
  kind: string
  detail: string
  weight: number
}) {
  return (
    <div
      className={cx(
        'mb-1.5 rounded-none border-l-2 bg-surface-2 px-2.5 py-2 text-xs leading-relaxed text-ink-2',
        weight > 0 ? 'border-l-ok' : weight < 0 ? 'border-l-crit' : 'border-l-line',
      )}
    >
      <span className="mb-0.5 block font-mono text-[10px] tracking-wide text-ink-mut">{kind}</span>
      {detail}
    </div>
  )
}

/* ── Empty + skeleton ─────────────────────────────────────────────── */

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 px-5 py-12 text-center text-ink-mut">
      <p className="m-0 max-w-[28ch] text-[12.5px]">{children}</p>
    </div>
  )
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx('animate-shimmer rounded-none', className)} />
}

export function SkeletonRows({ count = 6 }: { count?: number }) {
  return (
    <div className="flex flex-col gap-[7px]">
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} className="h-[74px] rounded-none" />
      ))}
    </div>
  )
}

/* ── Animated bar ─────────────────────────────────────────────────── */

/** Width animates from zero on mount, which reads as the value being
 *  measured rather than simply appearing. */
export function Bar({
  fraction, colour = 'var(--accent)', height = 5, className,
}: {
  fraction: number
  colour?: string
  height?: number
  className?: string
}) {
  return (
    <div
      className={cx('overflow-hidden rounded-none bg-surface-2', className)}
      style={{ height }}
    >
      <div
        className="h-full rounded-none transition-[width] duration-700 ease-[cubic-bezier(.22,.8,.28,1)]"
        style={{ width: `${Math.max(0, Math.min(100, fraction * 100))}%`, background: colour }}
      />
    </div>
  )
}
