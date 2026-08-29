import { useState } from 'react'
import type { DailyPoint } from '../types'

interface Hover {
  point: DailyPoint
  x: number
  y: number
}

/**
 * Matched versus open value per settlement date, as stacked bars.
 *
 * Hand-drawn SVG rather than a charting library. The shape is simple enough
 * that a dependency would cost more bundle than it saves code, and drawing it
 * directly means the bars inherit the theme tokens and re-colour instantly
 * when the theme flips, with no chart-level theme config to keep in sync.
 */
export function DailyChart({ data }: { data: DailyPoint[] }) {
  const [hover, setHover] = useState<Hover | null>(null)

  if (!data.length) return null

  const W = 760
  const H = 190
  const padX = 8
  const padT = 10
  const padB = 22
  const innerW = W - padX * 2
  const innerH = H - padT - padB

  const max = Math.max(...data.map((d) => d.matched + d.broken), 1)
  const slot = innerW / data.length
  const barW = Math.max(2, Math.min(20, slot * 0.62))

  // Label roughly every sixth day so the axis never collides with itself.
  const step = Math.max(1, Math.round(data.length / 6))

  return (
    <div className="relative w-full">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="block h-auto w-full overflow-visible"
        role="img"
        aria-label="Reconciled versus open value by settlement date"
        onMouseLeave={() => setHover(null)}
      >
        {[0.25, 0.5, 0.75, 1].map((f) => {
          const y = padT + innerH - innerH * f
          return (
            <line
              key={f}
              x1={padX}
              y1={y}
              x2={W - padX}
              y2={y}
              stroke="var(--line-soft)"
              strokeWidth={1}
            />
          )
        })}

        {data.map((d, i) => {
          const x = padX + slot * i + (slot - barW) / 2
          const hMatched = (d.matched / max) * innerH
          const hBroken = (d.broken / max) * innerH
          const yBroken = padT + innerH - hBroken
          const yMatched = yBroken - hMatched
          const dim = hover !== null && hover.point.date !== d.date

          return (
            <g
              key={d.date}
              className="cursor-pointer"
              style={{ opacity: dim ? 0.38 : 1, transition: 'opacity .15s' }}
              onMouseMove={(event) =>
                setHover({ point: d, x: event.clientX, y: event.clientY })
              }
            >
              <rect
                x={x}
                y={yMatched}
                width={barW}
                height={Math.max(hMatched, 0)}
                rx={1.5}
                fill="var(--ok)"
              />
              <rect
                x={x}
                y={yBroken}
                width={barW}
                height={Math.max(hBroken, 0)}
                rx={1.5}
                fill="var(--crit)"
              />
              {/* A full-slot transparent target, so thin bars stay hoverable. */}
              <rect
                x={padX + slot * i}
                y={padT}
                width={slot}
                height={innerH}
                fill="transparent"
              />
            </g>
          )
        })}

        {data.map((d, i) =>
          i % step === 0 ? (
            <text
              key={'label-' + d.date}
              x={padX + slot * i + slot / 2}
              y={H - 6}
              textAnchor="middle"
              className="font-mono"
              fill="var(--ink-mut)"
              fontSize={9.5}
            >
              {d.date.slice(5)}
            </text>
          ) : null,
        )}
      </svg>

      {hover && (
        <div
          role="tooltip"
          className="pointer-events-none fixed z-60 whitespace-nowrap rounded-md border border-line bg-surface-3 px-3 py-2 text-[11.5px] shadow-[var(--shadow-2)]"
          style={{
            left: Math.min(hover.x + 14, window.innerWidth - 190),
            top: Math.max(hover.y - 78, 8),
          }}
        >
          <b className="mb-1 block font-mono text-ink">{hover.point.date}</b>
          <span className="block font-mono text-ok">
            matched {hover.point.matched_display}
          </span>
          <span className="block font-mono text-crit">
            open {hover.point.broken_display}
          </span>
        </div>
      )}
    </div>
  )
}
