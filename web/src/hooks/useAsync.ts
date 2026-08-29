import { useCallback, useEffect, useRef, useState } from 'react'

export interface AsyncState<T> {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => void
}

/** Minimal data-fetching hook.
 *
 *  A query library would be more capable, but every request here is a plain
 *  GET against a batch that only changes on an explicit re-run, so caching and
 *  invalidation machinery would be weight without a job. What does matter is
 *  aborting in-flight requests: filters change on every keystroke, and a stale
 *  response landing after a newer one would show the wrong rows. */
export function useAsync<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)
  const mounted = useRef(true)

  useEffect(() => () => { mounted.current = false }, [])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)

    fn(controller.signal)
      .then((result) => {
        if (controller.signal.aborted || !mounted.current) return
        setData(result)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || !mounted.current) return
        setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (controller.signal.aborted || !mounted.current) return
        setLoading(false)
      })

    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  return { data, error, loading, reload }
}
