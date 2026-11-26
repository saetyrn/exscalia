// Geometry for the donut slices in Graphic.svelte.
//
// A slice is one closed path: the outer arc clockwise, a radial line in, the
// inner arc back, closed. That breaks for a slice holding the whole total
// (a single value, or [10, 0, 0]): its arc starts and ends on the same point,
// and SVG drops an arc whose endpoints coincide, so the ring drew blank while
// the legend still listed 100%. A near-full slice goes through the opposite
// point instead, as two arcs per ring, so every arc has distinct endpoints.

/** At or above this share of the total a slice is drawn as a full ring. */
const FULL = 0.9999;

/** The path for a slice covering `frac` of the ring, starting at angle `a0`. */
export function pieArcPath(frac: number, a0: number, cx: number, cy: number, R: number, r0: number): string {
	const a1 = a0 + frac * Math.PI * 2;
	const p = (a: number, rad: number) => `${cx + rad * Math.cos(a)},${cy + rad * Math.sin(a)}`;
	if (frac >= FULL) {
		const mid = a0 + Math.PI;
		return `M${p(a0, R)} A${R},${R} 0 0 1 ${p(mid, R)} A${R},${R} 0 0 1 ${p(a1, R)} L${p(a1, r0)} A${r0},${r0} 0 0 0 ${p(mid, r0)} A${r0},${r0} 0 0 0 ${p(a0, r0)} Z`;
	}
	const large = a1 - a0 > Math.PI ? 1 : 0;
	return `M${p(a0, R)} A${R},${R} 0 ${large} 1 ${p(a1, R)} L${p(a1, r0)} A${r0},${r0} 0 ${large} 0 ${p(a0, r0)} Z`;
}
