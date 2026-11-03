// Geometry for the revolving recipe menu, after saetyrn.com's RevolvingMenu: items ride an arc
// around the orb, x = R(cos a − 1), y = R sin a, with a drum tilt, and fade as they leave focus.

export const LINE = 1.2 // title line-height, in em

export interface Geometry {
  compact: boolean
  radius: number
  step: number // radians between neighbours
  stretch: number // scaleY applied to the serif titles
  fontMax: number // px
}

export interface ItemLayout {
  x: number
  y: number
  rotateX: number
  rotateY: number
  rotateZ: number
  scale: number
  opacity: number
}

export const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

/** Size the arc to the stage so two neighbours fit on each side, and cap titles so they never collide. */
export function geometry(width: number, height: number, compact: boolean): Geometry {
  const step = compact ? 0.6 : 0.5
  const stretch = compact ? 1.8 : 2.2
  const radius = compact ? clamp(height * 0.47, 110, 240) : clamp(height * 0.55, 180, 420)
  const gap = radius * Math.sin(step)
  // Active title plus a scaled neighbour, plus the meta line, must fit inside one gap.
  const fontMax = clamp((gap - 24) / (LINE * stretch * 0.92), 18, compact ? 46 : 84)
  void width
  return { compact, radius, step, stretch, fontMax }
}

/** Largest font size at which the title fits the stage width on one line. */
export function titleSize(title: string, width: number, g: Geometry): number {
  const chars = Math.max(6, title.trim().length)
  return clamp(width / (chars * 0.47), 18, g.fontMax)
}

/** Where an item sits for a given offset from focus (0 = in focus, may be fractional while dragging). */
export function itemLayout(offset: number, g: Geometry): ItemLayout {
  const a = offset * g.step
  const deg = (a * 180) / Math.PI
  const abs = Math.abs(offset)
  return {
    x: g.compact ? 0 : g.radius * (Math.cos(a) - 1),
    y: g.radius * Math.sin(a),
    rotateX: -deg * (g.compact ? 0.5 : 0.8),
    rotateY: g.compact ? 0 : offset * -2,
    rotateZ: g.compact ? 0 : deg * 0.15,
    scale: 1 - Math.min(abs, 3) * 0.08,
    opacity: Math.max(0, 1 - abs * (g.compact ? 0.4 : 0.42)),
  }
}

export function itemTransform(l: ItemLayout): string {
  return (
    `translate3d(${l.x.toFixed(2)}px, calc(${l.y.toFixed(2)}px - 50%), 0) ` +
    `rotateX(${l.rotateX.toFixed(2)}deg) rotateY(${l.rotateY.toFixed(2)}deg) rotateZ(${l.rotateZ.toFixed(2)}deg) ` +
    `scale(${l.scale.toFixed(3)})`
  )
}

/** Past either end the drag gives with resistance instead of stopping dead. */
export function rubberBand(pos: number, count: number): number {
  const max = Math.max(0, count - 1)
  if (pos < 0) return pos * 0.35
  if (pos > max) return max + (pos - max) * 0.35
  return pos
}

/**
 * Wheel to steps. Deltas accumulate within one gesture (a pause over 300 ms starts a new one) and a
 * step fires past 20 px, at most once per 120 ms, matching the site's feel for mouse and trackpad.
 */
export function createWheelStepper(threshold = 20, cooldown = 120, gestureGap = 300) {
  let acc = 0
  let lastEvent = -Infinity
  let lastStep = -Infinity
  return (deltaY: number, now: number): -1 | 0 | 1 => {
    if (now - lastEvent > gestureGap) acc = 0
    lastEvent = now
    acc += deltaY
    if (now - lastStep < cooldown || Math.abs(acc) <= threshold) return 0
    const dir = acc > 0 ? 1 : -1
    acc = 0
    lastStep = now
    return dir
  }
}
