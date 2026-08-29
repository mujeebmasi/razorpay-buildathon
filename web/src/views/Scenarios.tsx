import { api } from '../lib/api'
import { useAsync } from '../hooks/useAsync'
import { cx, num, pct } from '../lib/format'
import { Bar, Card, Skeleton } from '../components/primitives'
import type { Difficulty } from '../types'

const DIFFICULTY: Record<Difficulty, string> = {
  trivial: 'bg-ok-soft text-ok',
  routine: 'bg-accent-soft text-accent',
  hard: 'bg-warn-soft text-warn',
  unresolvable: 'bg-crit-soft text-crit',
}

const HEADINGS = ['scenario', 'difficulty', 'correct outcome', 'cases', 'accuracy'] as const

export function Scenarios() {
  const { data, loading, error } = useAsync((signal) => api.scenarios(signal), [])

  return (
    <Card
      title="The edge-case catalogue"
      blurb={
        <>
          Every scenario the batch deliberately contains, and how many cases the engine got right.
          Rows marked <em>unresolvable</em> cannot be matched from the data &mdash; being flagged is
          the correct outcome, and matching them would be a failure.
        </>
      }
    >
      {loading && !data ? (
        <Skeleton className="h-96 rounded-[10px]" />
      ) : error || !data ? (
        <p className="text-[12.5px] text-crit">Could not load the catalogue: {error}</p>
      ) : (
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[640px] border-collapse text-[12.5px]">
            <thead>
              <tr>
                {HEADINGS.map((h) => (
                  <th
                    key={h}
                    className={cx(
                      'whitespace-nowrap border-b border-line px-2.5 py-2 text-[10px] font-bold uppercase tracking-wider text-ink-mut',
                      h === 'cases' ? 'text-right' : 'text-left',
                    )}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.scenarios.map((s) => {
                const rate = s.cases ? s.correct / s.cases : 1
                return (
                  <tr key={s.key}>
                    <td className="border-b border-line-soft px-2.5 py-2.5 align-top">
                      <strong>{s.title}</strong>
                      <div className="mt-0.5 text-[11.5px] leading-snug text-ink-mut">
                        {s.description}
                      </div>
                    </td>
                    <td className="border-b border-line-soft px-2.5 py-2.5 align-top">
                      <span
                        className={cx(
                          'whitespace-nowrap rounded px-1.5 py-0.5 text-[9.5px] font-bold uppercase tracking-wider',
                          DIFFICULTY[s.difficulty],
                        )}
                      >
                        {s.difficulty}
                      </span>
                    </td>
                    <td className="border-b border-line-soft px-2.5 py-2.5 align-top font-mono text-[11px] text-ink-mut">
                      {s.disposition}
                      <br />
                      {s.expected_reason}
                    </td>
                    <td className="whitespace-nowrap border-b border-line-soft px-2.5 py-2.5 text-right align-top font-mono">
                      {num(s.cases)}
                    </td>
                    <td className="border-b border-line-soft px-2.5 py-2.5 align-top">
                      <div className="flex min-w-[120px] items-center gap-2.5">
                        <Bar
                          className="flex-1"
                          height={6}
                          fraction={rate}
                          colour={rate < 1 ? 'var(--warn)' : 'var(--ok)'}
                        />
                        <span className="w-9 text-right font-mono text-[11px]">{pct(rate, 0)}</span>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
