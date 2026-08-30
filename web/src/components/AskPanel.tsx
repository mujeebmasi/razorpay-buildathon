import { useEffect, useRef, useState } from 'react'

import { api } from '../lib/api'
import { answer, SUGGESTED_QUESTIONS, type Answer, type QaContext } from '../lib/qa'
import type { AgentPayload, ExceptionDetail, RunSummary } from '../types'

/* A settlement Q&A panel, docked so it is the second thing seen rather than
   something to hunt for.

   It answers from the run's own data. On a static deployment there is no
   backend to reach a model through, and putting a provider key in the browser
   to fake one would be a straightforwardly bad trade — so it looks figures up
   instead of generating them, and says plainly when a question is outside what
   it can look up. */

interface Turn {
  question: string
  answer: Answer
  source: 'data' | 'agent'
}

export function AskPanel({ run }: { run: RunSummary }) {
  const [open, setOpen] = useState(true)
  const [draft, setDraft] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [thinking, setThinking] = useState(false)
  const [ctx, setCtx] = useState<QaContext | null>(null)
  const logRef = useRef<HTMLDivElement>(null)

  // The panel needs the full register and the agent record to answer from.
  // Both are already cached by the client, so this costs nothing after the
  // relevant tab has been visited once.
  useEffect(() => {
    let alive = true
    Promise.all([
      api.exceptions({ severity: 'all', reason: 'all', q: '', offset: 0, limit: 1000 }),
      api.agent().catch(() => null),
    ]).then(([page, agent]) => {
      if (!alive) return
      setCtx({
        run,
        exceptions: page.items as ExceptionDetail[],
        agent: agent as AgentPayload | null,
      })
    })
    return () => { alive = false }
  }, [run])

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns, thinking])

  const ask = (question: string) => {
    const text = question.trim()
    if (!text || !ctx || thinking) return
    setDraft('')
    setThinking(true)
    // A beat before answering. Instant text reads as canned; this reads as
    // a lookup, which is what it is.
    window.setTimeout(() => {
      setTurns((t) => [...t, { question: text, answer: answer(text, ctx), source: 'data' }])
      setThinking(false)
    }, 260)
  }

  return (
    <aside
      className="border-t-2 border-x border-b border-line border-t-rule bg-surface xl:sticky xl:top-[92px]"
      aria-label="Ask about this reconciliation"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 border-b border-line px-3.5 py-2.5 text-left hover:bg-surface-2"
      >
        <span aria-hidden className="size-1.5 shrink-0 bg-accent" />
        <span className="flex-1 font-mono text-[11px] uppercase tracking-[0.14em] text-ink-2">
          Ask the register
        </span>
        <span className="font-mono text-[11px] text-ink-mut">{open ? '−' : '+'}</span>
      </button>

      {open && (
        <>
          <div
            ref={logRef}
            className="max-h-[46vh] overflow-y-auto px-3.5 py-3 xl:max-h-[52vh]"
          >
            {turns.length === 0 && (
              <p className="mb-3 text-[12.5px] leading-relaxed text-ink-2">
                Answers come from this run's own figures — nothing is generated. Ask
                about the money at risk, a reason code, what the agent did, or paste
                a record id.
              </p>
            )}

            {turns.map((turn, i) => (
              <div key={i} className="mb-4 last:mb-0">
                <p className="mb-1.5 border-l-2 border-accent pl-2.5 font-mono text-[12px] text-ink">
                  {turn.question}
                </p>
                <p className="text-[12.5px] leading-relaxed text-ink-2">
                  {turn.answer.text}
                </p>

                {turn.answer.citations.length > 0 && (
                  <dl className="banded mt-2 border border-line">
                    {turn.answer.citations.map((c, j) => (
                      <div
                        key={j}
                        className="flex items-baseline justify-between gap-3 border-b border-line-soft px-2.5 py-1.5 last:border-b-0"
                      >
                        <dt className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-ink-mut">
                          {c.label}
                        </dt>
                        <dd className="m-0 text-right font-mono text-[11.5px] tabular-nums text-ink">
                          {c.value}
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}

                {turn.answer.suggestions && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {turn.answer.suggestions.map((s) => (
                      <button
                        key={s}
                        type="button"
                        onClick={() => ask(s)}
                        className="border border-line px-2 py-1 font-mono text-[10.5px] text-ink-2 hover:border-accent hover:text-accent"
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ))}

            {thinking && (
              <p className="font-mono text-[11.5px] text-ink-mut">looking it up…</p>
            )}
          </div>

          {turns.length === 0 && (
            <div className="flex flex-wrap gap-1.5 border-t border-line px-3.5 py-2.5">
              {SUGGESTED_QUESTIONS.slice(0, 4).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => ask(s)}
                  className="border border-line px-2 py-1 font-mono text-[10.5px] text-ink-2 hover:border-accent hover:text-accent"
                >
                  {s}
                </button>
              ))}
            </div>
          )}

          <form
            onSubmit={(e) => { e.preventDefault(); ask(draft) }}
            className="flex items-center gap-2 border-t border-line px-3.5 py-2.5"
          >
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Ask about this run…"
              aria-label="Ask about this reconciliation"
              className="min-w-0 flex-1 border border-line bg-bg px-2.5 py-1.5 font-mono text-[12px] text-ink outline-none placeholder:text-ink-mut focus:border-accent"
            />
            <button
              type="submit"
              disabled={!draft.trim() || !ctx}
              className="border border-rule bg-ink px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.1em] text-surface disabled:opacity-40"
            >
              Ask
            </button>
          </form>
        </>
      )}
    </aside>
  )
}
