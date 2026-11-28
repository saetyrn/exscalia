import type { Unit } from './types'

export const UNITS: Unit[] = [
  '', 'tsp', 'tbsp', 'cup', 'ml', 'l', 'g', 'kg', 'oz', 'lb',
  'pinch', 'clove', 'slice', 'can', 'bunch', 'stick', 'sprig',
]

const PLURALS: Partial<Record<Unit, string>> = {
  cup: 'cups', pinch: 'pinches', clove: 'cloves', slice: 'slices',
  can: 'cans', bunch: 'bunches', stick: 'sticks', sprig: 'sprigs',
}

/** Unit as shown next to an amount. Count items have no unit. */
export function unitLabel(unit: Unit, amount: number | null): string {
  if (!unit) return ''
  if (amount !== null && amount > 1 && PLURALS[unit]) return PLURALS[unit] as string
  return unit
}

/** Unit name for the review form's select. */
export function unitName(unit: Unit): string {
  return unit === '' ? 'count' : unit
}

const PLURAL_EXCEPTIONS: Record<string, string> = {
  tomato: 'tomatoes', potato: 'potatoes', leaf: 'leaves', loaf: 'loaves', half: 'halves', child: 'children',
}
const SINGULAR_EXCEPTIONS: Record<string, string> = Object.fromEntries(
  Object.entries(PLURAL_EXCEPTIONS).map(([s, p]) => [p, s]),
)
const INVARIANT = new Set(['fish', 'shrimp', 'squid', 'salmon', 'tuna', 'tofu', 'bread', 'rice', 'pasta'])

function pluralizeWord(w: string): string {
  const lower = w.toLowerCase()
  if (INVARIANT.has(lower) || /s$/i.test(w)) return w
  if (PLURAL_EXCEPTIONS[lower]) return PLURAL_EXCEPTIONS[lower]
  if (/[^aeiou]y$/i.test(w)) return w.slice(0, -1) + 'ies'
  if (/(s|x|z|ch|sh)$/i.test(w)) return w + 'es'
  return w + 's'
}

function singularizeWord(w: string): string {
  const lower = w.toLowerCase()
  if (INVARIANT.has(lower) || !/s$/i.test(w) || /(ss|us|is)$/i.test(w)) return w
  if (SINGULAR_EXCEPTIONS[lower]) return SINGULAR_EXCEPTIONS[lower]
  if (/ies$/i.test(w) && w.length > 4) return w.slice(0, -3) + 'y'
  if (/(xes|zes|ches|shes|sses)$/i.test(w)) return w.slice(0, -2)
  return w.slice(0, -1)
}

/** Counted items follow their number: "1 egg", "2 eggs", "½ onion". Items with a unit are left alone. */
export function itemLabel(item: string, amount: number | null, unit: Unit): string {
  if (unit !== '' || amount === null || !item.trim()) return item
  const words = item.trim().split(/\s+/)
  const last = words.pop() as string
  words.push(amount > 1 ? pluralizeWord(last) : singularizeWord(last))
  return words.join(' ')
}
