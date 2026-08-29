/** Typed fetch layer. Every endpoint the dashboard uses, in one place. */
import type {
  ExceptionDetail, ExceptionPage, JournalPayload, MatchPage,
  RunSummary, ScenarioPayload, Severity,
} from '../types'

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal })
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`)
  return (await res.json()) as T
}

export const api = {
  run: (signal?: AbortSignal) => get<RunSummary>('/api/run', signal),

  exceptions: (
    params: { severity: string; reason: string; q: string; offset: number; limit?: number },
    signal?: AbortSignal,
  ) => {
    const query = new URLSearchParams({
      severity: params.severity,
      reason: params.reason,
      q: params.q,
      offset: String(params.offset),
      limit: String(params.limit ?? 60),
    })
    return get<ExceptionPage>(`/api/exceptions?${query}`, signal)
  },

  exception: (id: string, signal?: AbortSignal) =>
    get<ExceptionDetail>(`/api/exception/${encodeURIComponent(id)}`, signal),

  matches: (tier: string, signal?: AbortSignal) =>
    get<MatchPage>(`/api/matches?tier=${encodeURIComponent(tier)}&detail=1&limit=150`, signal),

  journal: (signal?: AbortSignal) => get<JournalPayload>('/api/journal?limit=25', signal),

  scenarios: (signal?: AbortSignal) => get<ScenarioPayload>('/api/scenarios', signal),

  refresh: () => get<{ ok: boolean; error: string | null }>('/api/refresh'),
}

/** Severity drives colour everywhere, so the mapping lives next to the client. */
export const SEVERITY_VAR: Record<Severity, string> = {
  critical: 'var(--crit)',
  high: 'var(--high)',
  medium: 'var(--warn)',
  low: 'var(--info)',
  info: 'var(--info)',
}
