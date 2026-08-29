import { useEffect, useState } from 'react'

/** Debounce a rapidly-changing value, so the search box does not fire a
 *  request per keystroke. */
export function useDebounced<T>(value: T, delay = 200): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return settled
}
