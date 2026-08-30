import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { cx } from '../lib/format'

const ToastContext = createContext<(message: string) => void>(() => {})

export const useToast = () => useContext(ToastContext)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<string | null>(null)
  const timer = useRef<number | undefined>(undefined)

  const push = useCallback((text: string) => {
    setMessage(text)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setMessage(null), 2600)
  }, [])

  const value = useMemo(() => push, [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        role="status"
        aria-live="polite"
        className={cx(
          'no-print pointer-events-none fixed bottom-7 left-1/2 z-90 max-w-[calc(100vw-2rem)]',
          'rounded-none border border-line bg-surface-3 px-4.5 py-2.5 text-[12.5px] text-ink',
          'shadow-[var(--shadow-3)] transition-all duration-200 ease-[cubic-bezier(.22,.8,.28,1)]',
          message ? 'translate-x-[-50%] translate-y-0 opacity-100' : 'translate-x-[-50%] translate-y-5 opacity-0',
        )}
      >
        {message}
      </div>
    </ToastContext.Provider>
  )
}
