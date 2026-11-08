import { describe, expect, it } from 'vitest'
import { createWheelStepper, geometry, itemLayout, rubberBand, titleSize } from './carousel'

describe('carousel geometry', () => {
  const wide = geometry(600, 700, false)
  const phone = geometry(340, 480, true)

  it('puts the focused item at the origin, fully opaque', () => {
    const l = itemLayout(0, wide)
    expect(l.x).toBeCloseTo(0)
    expect(l.y).toBeCloseTo(0)
    expect(l.opacity).toBe(1)
    expect(l.scale).toBe(1)
  })

  it('curves neighbours toward the orb on wide screens and stacks them on phones', () => {
    const below = itemLayout(1, wide)
    expect(below.y).toBeGreaterThan(0)
    expect(below.x).toBeLessThan(0)
    expect(itemLayout(-1, wide).y).toBeLessThan(0)
    expect(itemLayout(1, phone).x).toBe(0)
  })

  it('shows two neighbours each side and hides the third', () => {
    expect(itemLayout(1, wide).opacity).toBeGreaterThan(0.5)
    expect(itemLayout(2, wide).opacity).toBeGreaterThan(0)
    expect(itemLayout(3, wide).opacity).toBe(0)
    expect(itemLayout(-3, phone).opacity).toBe(0)
  })

  it('keeps titles apart: focused title plus a neighbour fit in one gap', () => {
    for (const g of [wide, phone, geometry(600, 420, false)]) {
      const gap = g.radius * Math.sin(g.step)
      const tall = g.fontMax * 1.2 * g.stretch
      expect(tall * 0.92 + 24).toBeLessThanOrEqual(gap + 0.5)
    }
  })

  it('shrinks long titles to fit the width, down to a readable floor', () => {
    expect(titleSize('Lemon garlic shrimp', 600, wide)).toBeCloseTo(wide.fontMax)
    const title = 'Tomato and white bean soup'
    const fs = titleSize(title, 340, phone)
    expect(fs).toBeLessThan(phone.fontMax)
    expect(fs * 0.47 * title.length).toBeLessThanOrEqual(341)
    // Past the floor the title is cut with an ellipsis in CSS rather than shrunk further.
    expect(titleSize('Slow-roasted tomato and white bean soup with crispy sage', 340, phone)).toBe(18)
  })

  it('rubber-bands past the ends', () => {
    expect(rubberBand(-1, 5)).toBeCloseTo(-0.35)
    expect(rubberBand(2.5, 5)).toBe(2.5)
    expect(rubberBand(5, 5)).toBeCloseTo(4.35)
  })
})

describe('wheel stepper', () => {
  it('accumulates small trackpad deltas within a gesture', () => {
    const step = createWheelStepper()
    expect(step(8, 0)).toBe(0)
    expect(step(8, 16)).toBe(0)
    expect(step(8, 32)).toBe(1)
  })
  it('respects the cooldown between steps', () => {
    const step = createWheelStepper()
    expect(step(100, 0)).toBe(1)
    expect(step(100, 50)).toBe(0)
    expect(step(100, 130)).toBe(1)
  })
  it('starts a new gesture after a pause and goes both ways', () => {
    const step = createWheelStepper()
    expect(step(15, 0)).toBe(0)
    expect(step(-15, 1000)).toBe(0)
    expect(step(-15, 1010)).toBe(-1)
  })
})
