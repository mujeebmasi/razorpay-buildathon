import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Moon, RefreshCw, Sun } from 'lucide-react'
import { cx } from '../lib/format'
import type { Theme } from '../hooks/useTheme'
import type { ViewName } from '../types'

const TABS: Array<{ id: ViewName; label: string }> = [
  { id: 'overview', label: 'Overview' },
  { id: 'exceptions', label: 'Exceptions' },
  { id: 'matches', label: 'Matches' },
  { id: 'journal', label: 'Journal' },
  { id: 'scenarios', label: 'Edge cases' },
]

export function AppBar({
  view,
  onView,
  adjudicator,
  counts,
  theme,
  onToggleTheme,
  onRerun,
  busy,
}: {
  view: ViewName
  onView: (v: ViewName) => void
  adjudicator: string
  counts: Partial<Record<ViewName, string>>
  theme: Theme
  onToggleTheme: () => void
  onRerun: () => void
  busy: boolean
}) {
  const listRef = useRef<HTMLDivElement>(null)
  const [ink, setInk] = useState({ left: 0, width: 0 })

  // The underline is positioned from the active tab's real geometry rather
  // than a fixed width, so it stays correct as labels and counts change.
  useEffect(() => {
    const move = () => {
      const active = listRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')
      if (active) setInk({ left: active.offsetLeft, width: active.offsetWidth })
    }
    move()
    window.addEventListener('resize', move)
    return () => window.removeEventListener('resize', move)
  }, [view, counts])

  const onTabKey = (event: KeyboardEvent) => {
    const index = TABS.findIndex((t) => t.id === view)
    if (event.key === 'ArrowRight') {
      event.preventDefault()
      onView(TABS[(index + 1) % TABS.length]!.id)
    }
    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      onView(TABS[(index - 1 + TABS.length) % TABS.length]!.id)
    }
  }

  return (
    <header className="no-print sticky top-0 z-40 border-b border-line bg-bg-elev/90 backdrop-blur-xl backdrop-saturate-150">
      <div className="flex items-center justify-between gap-3 px-3.5 py-3 sm:px-6 lg:px-7">
        <div className="flex min-w-0 items-center gap-3">
          <span
            aria-hidden
            className="grid size-9 shrink-0 place-items-center rounded-[9px] bg-gradient-to-br from-accent to-accent-2 text-[19px] font-semibold text-white shadow-[var(--shadow-1)]"
          >
            &#8377;
          </span>
          <span className="flex min-w-0 flex-col leading-tight">
            <strong className="text-[15.5px] tracking-tight">finctl</strong>
            <span className="hidden text-[11.5px] text-ink-mut sm:block">settlement controller</span>
          </span>
        </div>

        <div className="flex items-center gap-2">
          <span
            title={'Residual decided by ' + adjudicator}
            className="hidden max-w-[30vw] items-center gap-2 overflow-hidden text-ellipsis whitespace-nowrap rounded-full border border-line bg-surface px-2.5 py-1.5 font-mono text-[11.5px] text-ink-2 md:inline-flex"
          >
            <span className="size-[7px] shrink-0 rounded-full bg-ok shadow-[0_0_0_3px_var(--ok-soft)]" />
            {adjudicator}
          </span>

          <button
            type="button"
            onClick={onToggleTheme}
            aria-label="Switch colour theme"
            title="Theme (T)"
            className="grid size-9 cursor-pointer place-items-center rounded-lg border border-line bg-surface text-ink-2 transition hover:border-accent hover:bg-surface-2 hover:text-ink active:translate-y-px"
          >
            {theme === 'light' ? <Moon size={15} /> : <Sun size={15} />}
          </button>

          <button
            type="button"
            onClick={onRerun}
            disabled={busy}
            title="Re-run the pipeline (R)"
            className="flex cursor-pointer items-center gap-2 rounded-lg border border-line bg-surface px-2.5 py-2 text-[12.5px] font-medium text-ink-2 transition hover:border-accent hover:bg-surface-2 hover:text-ink active:translate-y-px disabled:pointer-events-none disabled:opacity-55 sm:px-3.5"
          >
            <RefreshCw size={15} className={busy ? 'animate-spin' : undefined} />
            <span className="hidden sm:inline">Re-run</span>
          </button>
        </div>
      </div>

      <div
        ref={listRef}
        role="tablist"
        aria-label="Sections"
        onKeyDown={onTabKey}
        className="relative flex gap-0.5 overflow-x-auto px-2 sm:px-5"
      >
        {TABS.map((tab) => {
          const active = tab.id === view
          return (
            <button
              key={tab.id}
              role="tab"
              id={'tab-' + tab.id}
              aria-controls={'view-' + tab.id}
              aria-selected={active}
              tabIndex={active ? 0 : -1}
              onClick={() => onView(tab.id)}
              className={cx(
                'relative cursor-pointer whitespace-nowrap border-0 bg-transparent px-3.5 pb-3.5 pt-2.5 text-[13.5px] transition-colors',
                active ? 'font-semibold text-ink' : 'text-ink-mut hover:text-ink-2',
              )}
            >
              {tab.label}
              {counts[tab.id] ? (
                <span
                  className={cx(
                    'ml-1.5 inline-block rounded-full px-[7px] align-[1px] font-mono text-[10.5px]',
                    active ? 'bg-accent-soft text-accent' : 'bg-surface-3 text-ink-mut',
                  )}
                >
                  {counts[tab.id]}
                </span>
              ) : null}
            </button>
          )
        })}
        <span
          aria-hidden
          className="absolute bottom-0 h-0.5 rounded-t-sm bg-accent transition-[transform,width] duration-300 ease-[cubic-bezier(.22,.8,.28,1)]"
          style={{ width: ink.width, transform: 'translateX(' + ink.left + 'px)' }}
        />
      </div>
    </header>
  )
}
