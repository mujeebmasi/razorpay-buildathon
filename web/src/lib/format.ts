/** Presentation helpers. Money arrives pre-formatted from the server, which
 *  is deliberate: the engine formats paise with Indian digit grouping and the
 *  frontend must never re-derive an amount it could get subtly wrong. */

export const pct = (value: number, digits = 1): string =>
  `${(value * 100).toFixed(digits)}%`

export const num = (value: number | undefined | null): string =>
  Number(value ?? 0).toLocaleString('en-IN')

export const titleise = (value: string): string => value.replace(/_/g, ' ')

export const TIER_NAMES: Record<string, string> = {
  T0: 'reference matched exactly',
  T1: 'reference recovered from narration',
  T2: 'unique amount inside the window',
  T3: 'within rounding tolerance',
  T4: 'gross recovered from the rate card',
  T5: 'batch decomposition',
  T6: 'global assignment',
  T7: 'adjudicated',
}

export const cx = (...parts: Array<string | false | null | undefined>): string =>
  parts.filter(Boolean).join(' ')

/** The engine writes ASCII `--` for an em dash so its terminal output stays
 *  encoding-safe. On screen that reads as a typo, so it is upgraded here —
 *  a presentation concern, fixed in the presentation layer. */
export const prose = (text: string): string => text.replace(/ -- /g, ' — ')
