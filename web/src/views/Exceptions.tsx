import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Search, X } from 'lucide-react'
import { api, SEVERITY_VAR } from '../lib/api'
import { cx, num, prose, titleise } from '../lib/format'
import { useDebounced } from '../hooks/useDebounced'
import { useIsNarrow } from '../hooks/useMediaQuery'
import { useToast } from '../components/Toast'
import { Empty, EvidenceItem, SeverityPill, SkeletonRows } from '../components/primitives'
import type { ExceptionDetail, ExceptionPage, ExceptionSummary, Severity } from '../types'

const SEVERITY_BORDER: Record<Severity, string> = {
  critical: 'border-l-crit',
  high: 'border-l-high',
  medium: 'border-l-warn',
  low: 'border-l-info',
  info: 'border-l-info',
}

/* ── the detail body, shared by the panel and the sheet ───────────── */

function CopyId({ value }: { value: string }) {
  const toast = useToast()
  return (
    <button
      type="button"
      title="Copy id"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value)
          toast('Copied ' + value)
        } catch {
          toast('Clipboard unavailable')
        }
      }}
      className="cursor-pointer rounded-none px-1 font-mono text-inherit text-ink-mut transition-colors hover:text-accent"
    >
      {value}
    </button>
  )
}

function SectionTitle({ children }: { children: string }) {
  return (
    <div className="mb-2 mt-4.5 border-b border-line-soft pb-1.5 text-[10px] font-bold uppercase tracking-[0.095em] text-ink-mut">
      {children}
    </div>
  )
}

function DetailBody({ item }: { item: ExceptionDetail | null }) {
  if (!item) {
    return (
      <Empty>Select an exception to see its full evidence trail.</Empty>
    )
  }

  return (
    <div>
      <h3 className="mb-1 text-[15px] font-semibold tracking-tight">{titleise(item.reason)}</h3>
      <div className="mb-4 flex flex-wrap items-center gap-2 font-mono text-[11.5px] text-ink-mut">
        <SeverityPill severity={item.severity} />
        <CopyId value={item.id} />
        <span>{item.amount_display}</span>
        <span>{item.as_of}</span>
      </div>

      <div className="rounded-none border border-accent/30 bg-accent-soft p-3.5 text-[12.5px] leading-relaxed text-ink-2">
        <strong className="mb-0.5 block text-accent">{item.owner}</strong>
        {prose(item.action)}
      </div>

      <SectionTitle>What happened</SectionTitle>
      <p className="m-0 text-[12.5px] leading-relaxed text-ink-2">{prose(item.summary)}</p>

      <SectionTitle>Facts considered</SectionTitle>
      {item.evidence.length ? (
        item.evidence.map((e, i) => (
          <EvidenceItem key={i} kind={e.kind} detail={prose(e.detail)} weight={e.weight} />
        ))
      ) : (
        <p className="text-xs text-ink-mut">No evidence recorded.</p>
      )}

      <SectionTitle>Details</SectionTitle>
      <dl className="m-0 grid grid-cols-[78px_1fr] gap-x-3 gap-y-1.5 text-xs sm:grid-cols-[92px_1fr]">
        <dt className="text-ink-mut">source</dt>
        <dd className="m-0 break-all font-mono text-ink-2">{item.source}</dd>
        <dt className="text-ink-mut">subjects</dt>
        <dd className="m-0 break-all font-mono text-ink-2">{item.subjects.join(', ')}</dd>
        {item.delta !== null && item.delta !== undefined ? (
          <>
            <dt className="text-ink-mut">delta</dt>
            <dd className="m-0 font-mono text-ink-2">{item.delta}</dd>
          </>
        ) : null}
        <dt className="text-ink-mut">examined</dt>
        <dd className="m-0 font-mono text-ink-2">{num(item.candidate_count)} candidate(s)</dd>
      </dl>

      {item.agent_trace?.length > 0 && (
        <>
          <SectionTitle>How the agent investigated</SectionTitle>
          <ol className="m-0 flex list-none flex-col gap-1.5 p-0">
            {item.agent_trace.map((step, i) => (
              <li
                key={i}
                className="flex items-baseline gap-2.5 rounded-none bg-surface-2 px-2.5 py-2 font-mono text-[11.5px] text-ink-2"
              >
                <span className="shrink-0 text-[10px] font-bold text-accent">
                  {String(i + 1).padStart(2, '0')}
                </span>
                <span className="break-all">{step}</span>
              </li>
            ))}
          </ol>
          <p className="mt-2 text-[11.5px] leading-snug text-ink-mut">
            The agent chose this sequence itself. These are the same primitives the
            deterministic cascade uses.
          </p>
        </>
      )}

      {item.records.length > 0 && (
        <>
          <SectionTitle>Underlying records</SectionTitle>
          {item.records.map((r) => (
            <div
              key={r.id}
              className="mb-1.5 rounded-none bg-surface-2 px-2.5 py-2 font-mono text-[11.5px] leading-relaxed text-ink-2"
            >
              <div className="mb-0.5 flex flex-wrap items-baseline gap-2">
                <span className="text-[9.5px] font-bold uppercase tracking-wider text-accent">
                  {r.kind}
                </span>
                <CopyId value={r.id} />
                <span>{r.amount}</span>
                <span>{r.date}</span>
              </div>
              <div className="break-words text-ink-mut">{r.detail}</div>
            </div>
          ))}
        </>
      )}
    </div>
  )
}

/* ── the view ─────────────────────────────────────────────────────── */

export function Exceptions({
  reasons,
  reasonFilter,
  onReasonFilter,
  registerNav,
}: {
  reasons: string[]
  reasonFilter: string
  onReasonFilter: (reason: string) => void
  /** Lets the app-level keyboard handler drive list selection. */
  registerNav: (move: ((delta: number) => void) | null) => void
}) {
  const isNarrow = useIsNarrow()

  const [severity, setSeverity] = useState('all')
  const [rawQuery, setRawQuery] = useState('')
  const query = useDebounced(rawQuery, 200)

  const [items, setItems] = useState<ExceptionSummary[]>([])
  const [page, setPage] = useState<ExceptionPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<ExceptionDetail | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)

  const searchRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  /* Filters reset the page; the effect refetches from offset zero. */
  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    setLoading(true)
    setError(null)

    api
      .exceptions({ severity, reason: reasonFilter, q: query, offset: 0 }, controller.signal)
      .then((data) => {
        if (cancelled) return
        setItems(data.items)
        setPage(data)
      })
      .catch((err: unknown) => {
        if (cancelled || controller.signal.aborted) return
        setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [severity, reasonFilter, query])

  const loadMore = useCallback(async () => {
    if (!page?.has_more) return
    const next = await api.exceptions({
      severity,
      reason: reasonFilter,
      q: query,
      offset: items.length,
    })
    setItems((current) => current.concat(next.items))
    setPage(next)
  }, [page, severity, reasonFilter, query, items.length])

  const select = useCallback(
    async (id: string) => {
      setSelected(id)
      setDetail(null)
      if (isNarrow) setSheetOpen(true)
      try {
        setDetail(await api.exception(id))
      } catch {
        setDetail(null)
      }
    },
    [isNarrow],
  )

  /* Expose j/k movement to the global key handler. */
  useEffect(() => {
    const move = (delta: number) => {
      if (!items.length) return
      const current = items.findIndex((it) => it.id === selected)
      const nextIndex = Math.max(
        0,
        Math.min(items.length - 1, (current < 0 ? -1 : current) + delta),
      )
      const next = items[nextIndex]
      if (!next) return
      listRef.current
        ?.querySelector<HTMLElement>('[data-id="' + CSS.escape(next.id) + '"]')
        ?.scrollIntoView({ block: 'nearest' })
      void select(next.id)
    }
    registerNav(move)
    return () => registerNav(null)
  }, [items, selected, select, registerNav])

  /* Focus the search box when the app-level handler fires "/". */
  useEffect(() => {
    const focus = () => searchRef.current?.focus()
    window.addEventListener('finctl:focus-search', focus)
    return () => window.removeEventListener('finctl:focus-search', focus)
  }, [])

  /* Close the sheet on Escape, and unlock scroll when it goes away. */
  useEffect(() => {
    if (!sheetOpen) {
      document.body.style.overflow = ''
      return
    }
    document.body.style.overflow = 'hidden'
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSheetOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
    }
  }, [sheetOpen])

  /* Leaving narrow mode with the sheet open would strand the scroll lock. */
  useEffect(() => {
    if (!isNarrow) setSheetOpen(false)
  }, [isNarrow])

  const severityBar = useMemo(() => {
    const counts = page?.severity_counts
    if (!counts) return []
    const order: Severity[] = ['critical', 'high', 'medium', 'low']
    const total = order.reduce((sum, k) => sum + (counts[k] ?? 0), 0) || 1
    return order
      .filter((k) => (counts[k] ?? 0) > 0)
      .map((k) => ({ key: k, width: ((counts[k] ?? 0) / total) * 100, count: counts[k] ?? 0 }))
  }, [page])

  return (
    <div>
      {/* toolbar */}
      <div className="no-print mb-3 flex flex-wrap items-center gap-2.5">
        <div className="relative flex min-w-[240px] flex-1 items-center">
          <Search size={15} className="pointer-events-none absolute left-3 text-ink-mut" />
          <input
            ref={searchRef}
            type="search"
            value={rawQuery}
            onChange={(e) => setRawQuery(e.target.value)}
            placeholder="Search summaries and record ids…"
            aria-label="Search exceptions"
            autoComplete="off"
            spellCheck={false}
            className="w-full rounded-none border border-line bg-surface py-2.5 pl-9 pr-10 text-[13px] transition-colors placeholder:text-ink-mut focus:border-accent focus:bg-bg-elev focus:outline-none"
          />
          <kbd className="kbd pointer-events-none absolute right-2.5 hidden sm:block">/</kbd>
        </div>

        <div className="flex w-full gap-2 sm:w-auto">
          <select
            aria-label="Severity"
            value={severity}
            onChange={(e) => setSeverity(e.target.value)}
            className="select min-w-0 flex-1 cursor-pointer rounded-none border border-line bg-surface py-2.5 pl-3 pr-8 text-[12.5px] text-ink-2 transition-colors hover:border-accent focus:border-accent focus:outline-none sm:flex-none"
          >
            <option value="all">All severities</option>
            <option value="critical">Critical only</option>
            <option value="high">High and above</option>
            <option value="medium">Medium and above</option>
          </select>

          <select
            aria-label="Reason"
            value={reasonFilter}
            onChange={(e) => onReasonFilter(e.target.value)}
            className="select min-w-0 flex-1 cursor-pointer rounded-none border border-line bg-surface py-2.5 pl-3 pr-8 text-[12.5px] text-ink-2 transition-colors hover:border-accent focus:border-accent focus:outline-none sm:flex-none"
          >
            <option value="all">All reasons</option>
            {reasons.map((r) => (
              <option key={r} value={r}>
                {titleise(r)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* severity distribution */}
      {severityBar.length > 0 && (
        <div className="mb-2.5 flex h-[5px] overflow-hidden rounded-none bg-surface-2" aria-hidden>
          {severityBar.map((s) => (
            <i
              key={s.key}
              title={s.count + ' ' + s.key}
              className="block h-full transition-[width] duration-500"
              style={{ width: s.width + '%', background: SEVERITY_VAR[s.key] }}
            />
          ))}
        </div>
      )}

      <p className="m-0 mb-3 font-mono text-xs text-ink-mut" aria-live="polite">
        {page
          ? num(page.total) +
            ' open · ' +
            page.total_value_display +
            ' · showing ' +
            num(items.length)
          : 'loading…'}
      </p>

      {/* list + detail */}
      <div className="grid items-start gap-3.5 lg:grid-cols-[minmax(0,1fr)_400px] 2xl:grid-cols-[minmax(0,1fr)_460px]">
        <div>
          {loading && items.length === 0 ? (
            <SkeletonRows count={6} />
          ) : error ? (
            <Empty>Could not load: {error}</Empty>
          ) : items.length === 0 ? (
            <Empty>Nothing matches these filters.</Empty>
          ) : (
            <div
              ref={listRef}
              role="listbox"
              aria-label="Exception register"
              className="banded flex flex-col border-x border-b border-line"
            >
              {items.map((it) => {
                const active = it.id === selected
                return (
                  <div
                    key={it.id}
                    role="option"
                    aria-selected={active}
                    tabIndex={0}
                    data-id={it.id}
                    onClick={() => void select(it.id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        void select(it.id)
                      }
                    }}
                    className={cx(
                      'cursor-pointer rounded-none border-b border-l-[3px] border-b-line-soft px-3.5 py-3 transition-colors duration-150',
                      'hover:bg-surface-3',
                      SEVERITY_BORDER[it.severity],
                      active ? 'bg-accent-soft' : '',
                    )}
                  >
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="flex min-w-0 flex-wrap items-center gap-2">
                        <SeverityPill severity={it.severity} />
                        <span className="font-mono text-[11.5px] text-accent">{it.reason}</span>
                      </span>
                      <span className="shrink-0 whitespace-nowrap font-mono text-[13px]">
                        {it.amount_display}
                      </span>
                    </div>
                    <div className="clamp-2 mt-1.5 text-[12.5px] leading-snug text-ink-2">
                      {prose(it.summary)}
                    </div>
                    <div className="mt-1.5 flex flex-wrap gap-2 font-mono text-[11px] text-ink-mut">
                      <span>{it.subjects.slice(0, 2).join(', ')}</span>
                      <span>{it.as_of}</span>
                      {it.candidate_count > 0 && (
                        <span>{num(it.candidate_count)} candidate(s) examined</span>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {page?.has_more && (
            <button
              type="button"
              onClick={() => void loadMore()}
              className="no-print mt-2.5 w-full cursor-pointer rounded-none border border-dashed border-line bg-surface py-3 text-[12.5px] text-ink-2 transition-colors hover:border-accent hover:text-accent"
            >
              Load more
            </button>
          )}
        </div>

        {/* desktop: docked panel. narrow: bottom sheet. */}
        {!isNarrow && (
          <aside
            className="sticky top-[124px] max-h-[calc(100vh-160px)] overflow-y-auto rounded-none border border-line bg-surface shadow-[var(--shadow-1)]"
            aria-label="Exception detail"
          >
            <div className="px-5 py-4.5">
              <DetailBody item={detail} />
            </div>
          </aside>
        )}
      </div>

      {isNarrow && sheetOpen && (
        <div className="fixed inset-0 z-70" aria-label="Exception detail" role="dialog" aria-modal>
          <div
            className="animate-fade absolute inset-0 backdrop-blur-[3px]"
            style={{ background: 'var(--scrim)' }}
            onClick={() => setSheetOpen(false)}
          />
          <div className="animate-sheet-up absolute inset-x-0 bottom-0 max-h-[88vh] overflow-y-auto rounded-t-[20px] border border-b-0 border-line bg-surface pb-[env(safe-area-inset-bottom)] shadow-[var(--shadow-3)]">
            <div className="sticky top-0 z-2 mx-auto mt-2.5 h-1 w-9 rounded-none bg-line" aria-hidden />
            <button
              type="button"
              onClick={() => setSheetOpen(false)}
              aria-label="Close detail"
              className="absolute right-3 top-2.5 z-3 grid size-8 cursor-pointer place-items-center rounded-none border border-line bg-surface-2 text-ink-2"
            >
              <X size={15} />
            </button>
            <div className="px-4.5 pb-6 pt-2">
              <DetailBody item={detail} />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
