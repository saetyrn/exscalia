// Donut slice paths. SVG omits an arc whose start and end points coincide, so
// a slice holding the whole total (a single value, or [10, 0, 0]) drew an
// empty ring while the legend said 100%. These pin the full-ring shape and
// that every other slice keeps the exact path it had before.
import { describe, expect, it } from 'vitest';
import { pieArcPath } from './piegeom';

// Graphic.svelte's donut: 240x240 viewBox, outer radius 92, hole radius 52.
const CX = 120,
	CY = 120,
	R = 92,
	R0 = 52;
const TOP = -Math.PI / 2;

type Arc = { r: number; large: number; sweep: number; from: [number, number]; to: [number, number] };

// The arcs of a path, each with the point it starts from.
function arcs(d: string): Arc[] {
	const out: Arc[] = [];
	let at: [number, number] = [NaN, NaN];
	for (const [, cmd, args] of d.matchAll(/([MALZ])([^MALZ]*)/g)) {
		const n = args.trim().split(/[\s,]+/).filter(Boolean).map(Number);
		if (cmd === 'M' || cmd === 'L') at = [n[0], n[1]];
		if (cmd === 'A') {
			const to: [number, number] = [n[5], n[6]];
			out.push({ r: n[0], large: n[3], sweep: n[4], from: at, to });
			at = to;
		}
	}
	return out;
}

const dist = (a: [number, number], b: [number, number]) => Math.hypot(a[0] - b[0], a[1] - b[1]);

// The formula Graphic.svelte used before the helper, for the unchanged cases.
function previousPath(frac: number, a0: number): string {
	const a1 = a0 + frac * Math.PI * 2;
	const large = a1 - a0 > Math.PI ? 1 : 0;
	const p = (a: number, rad: number) => `${CX + rad * Math.cos(a)},${CY + rad * Math.sin(a)}`;
	return `M${p(a0, R)} A${R},${R} 0 ${large} 1 ${p(a1, R)} L${p(a1, R0)} A${R0},${R0} 0 ${large} 0 ${p(a0, R0)} Z`;
}

describe('pieArcPath', () => {
	it('draws a 100% slice as four arcs with distinct endpoints', () => {
		const d = pieArcPath(1, TOP, CX, CY, R, R0);
		const a = arcs(d);
		expect(a).toHaveLength(4);
		for (const arc of a) expect(dist(arc.from, arc.to)).toBeGreaterThan(1);
		// Outer ring clockwise through the bottom, inner ring back the other way.
		expect(a.map((x) => [x.r, x.sweep])).toEqual([
			[R, 1],
			[R, 1],
			[R0, 0],
			[R0, 0]
		]);
		expect(a[0].from[0]).toBeCloseTo(CX);
		expect(a[0].from[1]).toBeCloseTo(CY - R);
		expect(a[0].to[1]).toBeCloseTo(CY + R);
		expect(a[2].to[1]).toBeCloseTo(CY + R0);
		expect(d).not.toContain('NaN');
	});

	it('the old single-arc path for a 100% slice had coinciding endpoints', () => {
		const a = arcs(previousPath(1, TOP));
		expect(a).toHaveLength(2);
		for (const arc of a) expect(dist(arc.from, arc.to)).toBeLessThan(1e-9);
	});

	it('treats a slice within 0.01% of the total as a full ring', () => {
		const a = arcs(pieArcPath(0.99995, TOP, CX, CY, R, R0));
		expect(a).toHaveLength(4);
		for (const arc of a) expect(dist(arc.from, arc.to)).toBeGreaterThan(1);
	});

	it('keeps the previous path for every partial slice', () => {
		for (const [frac, a0] of [
			[0.5, TOP],
			[0.25, TOP],
			[0.75, 0.3],
			[0.999, TOP],
			[0.001, 2]
		]) {
			expect(pieArcPath(frac, a0, CX, CY, R, R0)).toBe(previousPath(frac, a0));
		}
	});

	it('sets the large-arc flag only past half the ring', () => {
		expect(arcs(pieArcPath(0.25, TOP, CX, CY, R, R0)).map((x) => x.large)).toEqual([0, 0]);
		expect(arcs(pieArcPath(0.75, TOP, CX, CY, R, R0)).map((x) => x.large)).toEqual([1, 1]);
	});

	it('draws a zero slice as a degenerate path without NaN', () => {
		const d = pieArcPath(0, TOP, CX, CY, R, R0);
		expect(d).not.toContain('NaN');
		const a = arcs(d);
		expect(a).toHaveLength(2);
		for (const arc of a) expect(dist(arc.from, arc.to)).toBe(0);
	});
});
