// Portion scaling. Every amount is recalculated from the stored original, which is never overwritten.
//
//   shown amount = stored amount × chosen servings / base servings
//
// Rules, in order: multiply; switch units when that reads more cleanly; show volumes as kitchen
// fractions and weights as whole grams; round whole-number items and mark them; scale to-taste
// items and mark them as a starting point. Times, temperatures and pan sizes are left alone.

import type { Ingredient, Unit } from './types'
import { itemLabel, unitLabel } from './units'

export type Marker = 'estimated' | 'rounded' | 'to_taste'

export interface Scaled {
  /** Amount after rounding, in the display unit. Null when the ingredient has no amount. */
  amount: number | null
  unit: Unit
  /** The amount as text: "1½", "300", "". */
  amountText: string
  /** Amount plus unit: "1½ tbsp", "2", "300 g". */
  text: string
  markers: Marker[]
  /** Exact scaled amount in the stored unit. */
  raw: number | null
}

const EXACT = 0.025 // relative error that still counts as the same amount
const CLOSE = 0.06 // relative error we accept to use a larger unit

const KITCHEN = [0.25, 1 / 3, 0.5, 2 / 3, 0.75]
const GLYPHS: [number, string][] = [
  [0.125, '⅛'], [0.25, '¼'], [1 / 3, '⅓'], [0.5, '½'], [2 / 3, '⅔'], [0.75, '¾'],
]

type Kind = 'fraction' | 'integer' | 'decimal1' | 'decimal2'

interface UnitRule {
  kind: Kind
  /** Fraction parts this unit may show (for 'fraction' kinds). */
  allowed: number[]
  /** Below this amount, try a smaller unit instead. */
  min: number
}

const RULES: Record<Unit, UnitRule> = {
  '': { kind: 'fraction', allowed: KITCHEN, min: 0.25 },
  tsp: { kind: 'fraction', allowed: [0.125, ...KITCHEN], min: 0.125 },
  tbsp: { kind: 'fraction', allowed: [0.5], min: 1 },
  cup: { kind: 'fraction', allowed: KITCHEN, min: 0.25 },
  ml: { kind: 'integer', allowed: [], min: 1 },
  l: { kind: 'decimal2', allowed: [], min: 1 },
  g: { kind: 'integer', allowed: [], min: 1 },
  kg: { kind: 'decimal1', allowed: [], min: 1 },
  oz: { kind: 'fraction', allowed: [0.25, 0.5, 0.75], min: 0.25 },
  lb: { kind: 'fraction', allowed: [0.25, 0.5, 0.75], min: 0.25 },
  pinch: { kind: 'fraction', allowed: KITCHEN, min: 0.25 },
  clove: { kind: 'fraction', allowed: KITCHEN, min: 0.25 },
  slice: { kind: 'fraction', allowed: KITCHEN, min: 0.25 },
  can: { kind: 'fraction', allowed: KITCHEN, min: 0.25 },
  bunch: { kind: 'fraction', allowed: KITCHEN, min: 0.25 },
  stick: { kind: 'fraction', allowed: KITCHEN, min: 0.25 },
  sprig: { kind: 'fraction', allowed: KITCHEN, min: 0.25 },
}

/** A fractional amount of a larger unit replaces a whole smaller amount only from this value up. */
const PROMOTE: Partial<Record<Unit, number>> = { cup: 0.5, lb: 0.25 }
const promoteFor = (u: Unit) => PROMOTE[u] ?? 1

/** Unit chains, smallest first, with the factor to the chain's base unit. */
const CHAINS: { units: Unit[]; factors: number[] }[] = [
  { units: ['tsp', 'tbsp', 'cup'], factors: [1, 3, 48] },
  { units: ['ml', 'l'], factors: [1, 1000] },
  { units: ['g', 'kg'], factors: [1, 1000] },
  { units: ['oz', 'lb'], factors: [1, 16] },
]

function snap(amount: number, rule: UnitRule): number {
  switch (rule.kind) {
    case 'integer':
      return Math.max(1, Math.round(amount))
    case 'decimal1':
      return Math.round(amount * 10) / 10
    case 'decimal2':
      return Math.round(amount * 100) / 100
    case 'fraction': {
      const whole = Math.floor(amount)
      const part = amount - whole
      let best = 0
      let bestErr = part
      for (const f of [...rule.allowed, 1]) {
        const err = Math.abs(part - f)
        if (err < bestErr) {
          best = f
          bestErr = err
        }
      }
      const value = whole + best
      return value === 0 ? rule.allowed[0] ?? 1 : value
    }
  }
}

function relError(amount: number, snapped: number): number {
  return Math.abs(snapped - amount) / Math.max(amount, 1e-9)
}

/** Format a snapped amount: "1½", "300", "1.5". */
export function formatAmount(value: number, unit: Unit): string {
  const rule = RULES[unit]
  if (rule.kind === 'integer') return String(Math.round(value))
  if (rule.kind === 'decimal1' || rule.kind === 'decimal2') {
    return String(Number(value.toFixed(rule.kind === 'decimal1' ? 1 : 2)))
  }
  const whole = Math.floor(value + 1e-9)
  const part = value - whole
  if (part < 1e-6) return String(whole)
  const glyph = GLYPHS.find(([f]) => Math.abs(f - part) < 1e-6)?.[1]
  if (!glyph) return String(Number(value.toFixed(2)))
  return whole > 0 ? `${whole}${glyph}` : glyph
}

interface Pick { unit: Unit; amount: number }

/** Choose the unit and rounded amount that read most cleanly for a scaled amount. */
export function pickDisplay(raw: number, unit: Unit, keepUnit = false): Pick {
  const chain = CHAINS.find((c) => c.units.includes(unit))
  if (!chain || keepUnit) {
    return { unit, amount: snap(raw, RULES[unit]) }
  }
  const base = raw * chain.factors[chain.units.indexOf(unit)]
  const candidates = chain.units.map((u, i) => {
    const amount = base / chain.factors[i]
    const snapped = snap(amount, RULES[u])
    return { unit: u, amount, snapped, err: relError(amount, snapped), ok: amount >= RULES[u].min }
  })
  const usable = candidates.filter((c) => c.ok)
  const isWhole = (n: number) => Math.abs(n - Math.round(n)) < 1e-6
  // Largest unit within tolerance, but a fractional larger unit does not beat a whole number of
  // the smaller unit until it reaches PROMOTE (so 4 tbsp stays 4 tbsp; 8 tbsp becomes ½ cup).
  const largestWithin = (tol: number) => {
    for (let i = candidates.length - 1; i >= 0; i--) {
      const c = candidates[i]
      if (!c.ok || c.err > tol) continue
      const smaller = candidates[i - 1]
      if (!smaller || isWhole(c.snapped) || !isWhole(smaller.amount) || c.snapped >= promoteFor(c.unit)) return c
    }
    return undefined
  }
  const pick =
    largestWithin(EXACT) ??
    largestWithin(CLOSE) ??
    usable.sort((a, b) => a.err - b.err)[0] ??
    candidates[0]
  return { unit: pick.unit, amount: pick.snapped }
}

/** Scale one ingredient by a factor (chosen servings / base servings). */
export function scaleIngredient(ing: Ingredient, factor: number): Scaled {
  const markers: Marker[] = []
  if (ing.estimated) markers.push('estimated')

  if (ing.amount === null) {
    if (ing.scaling === 'to_taste') markers.push('to_taste')
    return { amount: null, unit: ing.unit, amountText: '', text: '', markers, raw: null }
  }

  const raw = ing.amount * factor

  if (ing.scaling === 'whole') {
    const n = Math.max(1, Math.round(raw))
    if (Math.abs(n - raw) > 1e-6) markers.push('rounded')
    const label = unitLabel(ing.unit, n)
    return { amount: n, unit: ing.unit, amountText: String(n), text: label ? `${n} ${label}` : String(n), markers, raw }
  }

  const picked = pickDisplay(raw, ing.unit, factor === 1)
  if (ing.scaling === 'to_taste' && factor !== 1) markers.push('to_taste')
  const amountText = formatAmount(picked.amount, picked.unit)
  const label = unitLabel(picked.unit, picked.amount)
  return {
    amount: picked.amount,
    unit: picked.unit,
    amountText,
    text: label ? `${amountText} ${label}` : amountText,
    markers,
    raw,
  }
}

/** Times, temperatures and pan sizes are not scaled; remind the cook when the factor is large. */
export function needsTimeReminder(factor: number): boolean {
  return factor >= 2 || factor <= 0.5
}

export type StepSegment =
  | { kind: 'text'; text: string }
  | { kind: 'ref'; ingredient: Ingredient; scaled: Scaled; text: string }
  | { kind: 'missing'; text: string }

/** Split a step into text and live ingredient references ({butter} → "4 tbsp butter"). */
export function renderStep(text: string, ingredients: Ingredient[], factor: number): StepSegment[] {
  const segments: StepSegment[] = []
  // [^{}]: from a "{" that never closes, [^}] rescanned to the end of the
  // step, quadratic in a run of them (an imported step is the page's text).
  const re = /\{([^{}]+)\}/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) segments.push({ kind: 'text', text: text.slice(last, m.index) })
    const key = m[1].trim().toLowerCase()
    const ing =
      ingredients.find((i) => i.id.toLowerCase() === key) ??
      ingredients.find((i) => i.item.toLowerCase() === key)
    if (ing) {
      const scaled = scaleIngredient(ing, factor)
      const name = itemLabel(ing.item, scaled.amount, scaled.unit)
      const label = scaled.text ? `${scaled.text} ${name}` : name
      segments.push({ kind: 'ref', ingredient: ing, scaled, text: label })
    } else {
      segments.push({ kind: 'missing', text: m[1] })
    }
    last = m.index + m[0].length
  }
  if (last < text.length) segments.push({ kind: 'text', text: text.slice(last) })
  return segments
}
