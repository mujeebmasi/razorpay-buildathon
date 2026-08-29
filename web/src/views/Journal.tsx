import { api } from '../lib/api'
import { useAsync } from '../hooks/useAsync'
import { Card, Note, Skeleton } from '../components/primitives'

export function Journal() {
  const { data, loading, error } = useAsync((signal) => api.journal(signal), [])

  if (loading && !data) {
    return (
      <div className="grid grid-cols-12 gap-3.5">
        <div className="col-span-12 lg:col-span-5">
          <Skeleton className="h-80 rounded-[14px]" />
        </div>
        <div className="col-span-12 lg:col-span-7">
          <Skeleton className="h-80 rounded-[14px]" />
        </div>
      </div>
    )
  }

  if (error || !data) {
    return <p className="text-[12.5px] text-crit">Could not load the journal: {error}</p>
  }

  return (
    <div className="grid grid-cols-12 gap-3.5">
      <Card span={5} title="Trial balance" blurb="Posted from verified matches only.">
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead>
              <tr>
                <th className="border-b border-line px-2.5 py-2 text-left text-[10px] font-bold uppercase tracking-wider text-ink-mut">
                  account
                </th>
                <th className="border-b border-line px-2.5 py-2 text-right text-[10px] font-bold uppercase tracking-wider text-ink-mut">
                  amount
                </th>
                <th className="border-b border-line px-2.5 py-2 text-left text-[10px] font-bold uppercase tracking-wider text-ink-mut">
                  dr/cr
                </th>
              </tr>
            </thead>
            <tbody>
              {data.trial_balance.map((row) => (
                <tr key={row.account}>
                  <td className="border-b border-line-soft px-2.5 py-2.5 align-top">
                    {row.account}
                  </td>
                  <td className="whitespace-nowrap border-b border-line-soft px-2.5 py-2.5 text-right font-mono">
                    {row.amount}
                  </td>
                  <td className="border-b border-line-soft px-2.5 py-2.5">{row.direction}</td>
                </tr>
              ))}
              <tr>
                <td className="border-t-2 border-line px-2.5 pb-2 pt-3 font-semibold">
                  total debits
                </td>
                <td className="whitespace-nowrap border-t-2 border-line px-2.5 pb-2 pt-3 text-right font-mono font-semibold">
                  {data.debits}
                </td>
                <td className="border-t-2 border-line" />
              </tr>
              <tr>
                <td className="px-2.5 py-1 font-semibold">total credits</td>
                <td className="whitespace-nowrap px-2.5 py-1 text-right font-mono font-semibold">
                  {data.credits}
                </td>
                <td />
              </tr>
            </tbody>
          </table>
        </div>

        <Note tone={data.balanced ? 'ok' : 'warn'}>
          {data.balanced ? (
            <>
              <strong className="text-ok">Balanced.</strong> Debits equal credits across every
              posted entry.
            </>
          ) : (
            <>
              <strong className="text-warn">Out of balance.</strong> Investigate before relying on
              this run.
            </>
          )}
        </Note>
      </Card>

      <Card
        span={7}
        title="Journal entries"
        blurb="Entry ids derive from the match, so re-running cannot double-post."
      >
        {data.entries.map((entry) => (
          <div
            key={entry.id}
            className="mb-2.5 rounded-[10px] border border-line bg-surface-2 px-3.5 py-3"
          >
            <div className="mb-1.5 flex flex-wrap justify-between gap-2.5 font-mono text-[11px] text-ink-mut">
              <span>{entry.id}</span>
              <span>{entry.date}</span>
            </div>
            <p className="m-0 mb-2.5 text-[12.5px] leading-snug text-ink-2">{entry.narrative}</p>
            {entry.lines.map((line, i) => (
              <div
                key={i}
                className="grid grid-cols-[26px_1fr_auto] gap-2 py-0.5 font-mono text-[11.5px]"
              >
                <span className={line.direction === 'Dr' ? 'text-ok' : 'pl-2.5 text-high'}>
                  {line.direction}
                </span>
                <span className="text-ink-2">{line.account}</span>
                <span className="text-right">{line.amount}</span>
              </div>
            ))}
          </div>
        ))}
      </Card>
    </div>
  )
}
