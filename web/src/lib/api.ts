/**
 * Typed fetch layer. Every endpoint the dashboard uses, in one place.
 *
 * It works two ways from one build. Served by `python -m finctl serve`, it
 * talks to the live API. Served as static files — a CDN, GitHub Pages, an
 * `unzip` and a double-click — it reads the JSON snapshots that
 * `python -m finctl export` writes, and does the filtering the server would
 * have done in the browser instead.
 *
 * The mode is probed once and remembered, rather than baked in at build time,
 * so the same `dist/` is correct in both places and there is no way to deploy
 * the wrong variant.
 */
import type {
  AgentPayload,
  ExceptionDetail, ExceptionPage, JournalPayload, MatchPage, MatchRow,
  RunSummary, ScenarioPayload, Severity,
} from '../types'

const STATIC_ROOT = 'data'

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal })
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`)
  return (await res.json()) as T
}

/* ── mode detection ───────────────────────────────────────────────────
   One HEAD-ish probe, cached as a promise so concurrent callers share it
   rather than racing to answer the same question.                     */

let modeProbe: Promise<boolean> | null = null

function isLive(): Promise<boolean> {
  modeProbe ??= fetch('/api/run', { method: 'GET' })
    .then((res) => res.ok)
    .catch(() => false)
  return modeProbe
}

/** Snapshots are fetched once each and reused; they never change mid-session. */
const snapshots = new Map<string, Promise<unknown>>()

function snapshot<T>(name: string): Promise<T> {
  if (!snapshots.has(name)) {
    snapshots.set(name, get<T>(`${STATIC_ROOT}/${name}.json`))
  }
  return snapshots.get(name) as Promise<T>
}

/* ── the filtering the server does, for when there is no server ──────── */

const SEVERITY_RANK: Record<Severity, number> = {
  critical: 0, high: 1, medium: 2, low: 3, info: 4,
}

function filterExceptions(
  all: ExceptionDetail[],
  p: { severity: string; reason: string; q: string; offset: number; limit: number },
): ExceptionPage {
  let items = all
  if (p.severity !== 'all') {
    const ceiling = SEVERITY_RANK[p.severity as Severity] ?? 4
    items = items.filter((e) => SEVERITY_RANK[e.severity] <= ceiling)
  }
  if (p.reason !== 'all') items = items.filter((e) => e.reason === p.reason)
  if (p.q.trim()) {
    const needle = p.q.trim().toLowerCase()
    items = items.filter(
      (e) =>
        e.summary.toLowerCase().includes(needle) ||
        e.subjects.some((s) => s.toLowerCase().includes(needle)),
    )
  }

  const totalValue = items.reduce((sum, e) => sum + e.amount, 0)
  const severityCounts = items.reduce<Record<string, number>>((acc, e) => {
    acc[e.severity] = (acc[e.severity] ?? 0) + 1
    return acc
  }, {})
  const page = items.slice(p.offset, p.offset + p.limit)

  return {
    total: items.length,
    offset: p.offset,
    returned: page.length,
    has_more: p.offset + page.length < items.length,
    total_value: totalValue,
    total_value_display: formatInr(totalValue),
    severity_counts: severityCounts as Record<Severity, number>,
    items: page,
  }
}

/**
 * Indian digit grouping, for the few totals computed in the browser.
 *
 * Everything else arrives pre-formatted from Python, which is deliberate —
 * money formatting lives next to money arithmetic. This exists only because a
 * filtered subtotal cannot be known ahead of time.
 */
function formatInr(paise: number): string {
  const sign = paise < 0 ? '-' : ''
  const whole = Math.floor(Math.abs(paise) / 100)
  const frac = String(Math.abs(paise) % 100).padStart(2, '0')
  const digits = String(whole)
  let grouped = digits
  if (digits.length > 3) {
    const tail = digits.slice(-3)
    let head = digits.slice(0, -3)
    const groups: string[] = []
    while (head.length > 2) {
      groups.unshift(head.slice(-2))
      head = head.slice(0, -2)
    }
    if (head) groups.unshift(head)
    grouped = [...groups, tail].join(',')
  }
  return `${sign}₹${grouped}.${frac}`
}

/* ── the client ───────────────────────────────────────────────────────── */

export const api = {
  run: async (signal?: AbortSignal) =>
    (await isLive())
      ? get<RunSummary>('/api/run', signal)
      : snapshot<RunSummary>('run'),

  exceptions: async (
    params: { severity: string; reason: string; q: string; offset: number; limit?: number },
    signal?: AbortSignal,
  ) => {
    const limit = params.limit ?? 60
    if (await isLive()) {
      const query = new URLSearchParams({
        severity: params.severity,
        reason: params.reason,
        q: params.q,
        offset: String(params.offset),
        limit: String(limit),
      })
      return get<ExceptionPage>(`/api/exceptions?${query}`, signal)
    }
    const all = await snapshot<ExceptionDetail[]>('exceptions')
    return filterExceptions(all, { ...params, limit })
  },

  exception: async (id: string, signal?: AbortSignal) => {
    if (await isLive()) {
      return get<ExceptionDetail>(`/api/exception/${encodeURIComponent(id)}`, signal)
    }
    const all = await snapshot<ExceptionDetail[]>('exceptions')
    const found = all.find((e) => e.id === id)
    if (!found) throw new Error(`no exception ${id}`)
    return found
  },

  matches: async (tier: string, signal?: AbortSignal) => {
    if (await isLive()) {
      return get<MatchPage>(
        `/api/matches?tier=${encodeURIComponent(tier)}&detail=1&limit=150`, signal,
      )
    }
    const all = await snapshot<MatchRow[]>('matches')
    const items = tier === 'all' ? all : all.filter((m) => m.tier === tier)
    return { total: items.length, items: items.slice(0, 150) }
  },

  journal: async (signal?: AbortSignal) =>
    (await isLive())
      ? get<JournalPayload>('/api/journal?limit=25', signal)
      : snapshot<JournalPayload>('journal'),

  agent: async (signal?: AbortSignal) =>
    (await isLive())
      ? get<AgentPayload>('/api/agent', signal)
      : snapshot<AgentPayload>('agent'),

  scenarios: async (signal?: AbortSignal) =>
    (await isLive())
      ? get<ScenarioPayload>('/api/scenarios', signal)
      : snapshot<ScenarioPayload>('scenarios'),

  /** Re-running needs the engine, so it is a no-op on a static deployment. */
  refresh: async () => {
    if (!(await isLive())) {
      return { ok: false, error: 'This is a static snapshot. Clone the repo and run '
                              + '`python -m finctl serve` to re-run the engine.' }
    }
    return get<{ ok: boolean; error: string | null }>('/api/refresh')
  },

  isStatic: async () => !(await isLive()),
}

/** Severity drives colour everywhere, so the mapping lives next to the client. */
export const SEVERITY_VAR: Record<Severity, string> = {
  critical: 'var(--crit)',
  high: 'var(--high)',
  medium: 'var(--warn)',
  low: 'var(--info)',
  info: 'var(--info)',
}
