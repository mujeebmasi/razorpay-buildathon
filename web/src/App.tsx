import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './lib/api'
import { num } from './lib/format'
import { useAsync } from './hooks/useAsync'
import { useTheme } from './hooks/useTheme'
import { AppBar } from './components/AppBar'
import { KpiRow, KpiSkeleton } from './components/KpiRow'
import { Skeleton, SkeletonRows } from './components/primitives'
import { useToast } from './components/Toast'
import { Overview } from './views/Overview'
import { Agent } from './views/Agent'
import { Exceptions } from './views/Exceptions'
import { Matches } from './views/Matches'
import { Journal } from './views/Journal'
import { Scenarios } from './views/Scenarios'
import type { ViewName } from './types'

const VIEW_ORDER: ViewName[] = ['overview', 'agent', 'exceptions', 'matches', 'journal', 'scenarios']

/** Loads the agent payload on demand, so the tab costs nothing until opened. */
function AgentView() {
  const { data, error, loading } = useAsync((signal) => api.agent(signal), [])
  if (loading) return <SkeletonRows count={5} />
  if (error) return <p className="text-[13px] text-crit">{error}</p>
  if (!data) return null
  return <Agent data={data} />
}

export function App() {
  const { theme, toggle } = useTheme()
  const toast = useToast()

  const [view, setView] = useState<ViewName>('overview')
  const [reasonFilter, setReasonFilter] = useState('all')
  const [busy, setBusy] = useState(false)

  const { data: run, error, loading, reload } = useAsync((signal) => api.run(signal), [])

  /* The exceptions list registers a mover so global j/k can drive it without
     the key handler needing to know anything about that view's internals. */
  const navRef = useRef<((delta: number) => void) | null>(null)
  const registerNav = useCallback((move: ((delta: number) => void) | null) => {
    navRef.current = move
  }, [])

  const rerun = useCallback(async () => {
    if (busy) return
    setBusy(true)
    toast('Re-running the pipeline…')
    try {
      const result = await api.refresh()
      if (!result.ok) throw new Error(result.error ?? 'run failed')
      reload()
      toast('Reconciliation complete')
    } catch (err) {
      toast('Failed: ' + (err instanceof Error ? err.message : String(err)))
    } finally {
      setBusy(false)
    }
  }, [busy, reload, toast])

  /* Global shortcuts. Everything here is a single unmodified key, so the
     handler bows out whenever focus is in a field or a modifier is held. */
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const tag = (document.activeElement as HTMLElement | null)?.tagName ?? ''
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag)

      if (event.key === '/' && !typing) {
        event.preventDefault()
        setView('exceptions')
        // The view mounts its input on the next frame.
        requestAnimationFrame(() =>
          window.dispatchEvent(new CustomEvent('finctl:focus-search')),
        )
        return
      }

      if (typing || event.metaKey || event.ctrlKey || event.altKey) return

      if (event.key === 't' || event.key === 'T') {
        toggle()
        return
      }
      if (event.key === 'r' || event.key === 'R') {
        void rerun()
        return
      }
      if (/^[1-5]$/.test(event.key)) {
        setView(VIEW_ORDER[Number(event.key) - 1]!)
        return
      }
      if (view === 'exceptions' && navRef.current) {
        if (event.key === 'j' || event.key === 'ArrowDown') {
          event.preventDefault()
          navRef.current(1)
        }
        if (event.key === 'k' || event.key === 'ArrowUp') {
          event.preventDefault()
          navRef.current(-1)
        }
      }
    }

    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [view, toggle, rerun])

  const reasons = useMemo(
    () => (run ? Object.keys(run.reason_counts).sort() : []),
    [run],
  )
  const tiers = useMemo(() => (run ? Object.keys(run.tier_counts).sort() : []), [run])

  const counts = useMemo<Partial<Record<ViewName, string>>>(
    () =>
      run
        ? { exceptions: num(run.exceptions), matches: num(run.matches) }
        : {},
    [run],
  )

  const pickReason = useCallback((reason: string) => {
    setReasonFilter(reason)
    setView('exceptions')
  }, [])

  return (
    <>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-100 focus:rounded-md focus:bg-accent focus:px-4 focus:py-2.5 focus:font-semibold focus:text-white"
      >
        Skip to content
      </a>

      <AppBar
        view={view}
        onView={setView}
        adjudicator={run?.adjudicator ?? 'loading…'}
        counts={counts}
        theme={theme}
        onToggleTheme={toggle}
        onRerun={() => void rerun()}
        busy={busy}
      />

      <main id="main" className="px-3.5 pb-16 pt-4 sm:px-6 lg:px-7">
        {error ? (
          <div className="card">
            <h2 className="mb-1 text-[13.5px] font-semibold">Could not load the run</h2>
            <p className="m-0 text-[12.5px] text-ink-mut">{error}</p>
          </div>
        ) : loading && !run ? (
          <>
            <KpiSkeleton />
            <div className="grid grid-cols-12 gap-3.5">
              <Skeleton className="col-span-12 h-64 rounded-[14px] xl:col-span-8" />
              <Skeleton className="col-span-12 h-64 rounded-[14px] xl:col-span-4" />
            </div>
          </>
        ) : run ? (
          <>
            {view === 'overview' && <KpiRow run={run} />}

            <div
              id={'view-' + view}
              role="tabpanel"
              aria-labelledby={'tab-' + view}
              className="animate-rise"
            >
              {view === 'overview' && <Overview run={run} onPickReason={pickReason} />}
              {view === 'exceptions' && (
                <Exceptions
                  reasons={reasons}
                  reasonFilter={reasonFilter}
                  onReasonFilter={setReasonFilter}
                  registerNav={registerNav}
                />
              )}
              {view === 'agent' && <AgentView />}
              {view === 'matches' && <Matches tiers={tiers} />}
              {view === 'journal' && <Journal />}
              {view === 'scenarios' && <Scenarios />}
            </div>
          </>
        ) : null}
      </main>

      <footer className="no-print flex flex-wrap justify-between gap-3.5 border-t border-line px-3.5 py-4 font-mono text-[11px] text-ink-mut sm:px-6 lg:px-7">
        <span>
          {run
            ? 'zero dependencies · ' +
              num(run.records) +
              ' records · ' +
              run.seconds.toFixed(2) +
              's · deterministic'
            : 'zero dependencies · deterministic'}
        </span>
        <span className="hidden items-center gap-1.5 md:flex">
          <kbd className="kbd">/</kbd> search
          <kbd className="kbd">J</kbd>
          <kbd className="kbd">K</kbd> move
          <kbd className="kbd">T</kbd> theme
          <kbd className="kbd">R</kbd> re-run
        </span>
      </footer>
    </>
  )
}
