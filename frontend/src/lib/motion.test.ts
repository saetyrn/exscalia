// Reduce Motion reaching Svelte's own transitions (a11y-mobile-design#9).
// Svelte 5 plays fly/fade/slide/flip through element.animate(), which the
// prefers-reduced-motion block in app.css cannot touch, so $lib/motion decides
// per call: a zero-length config with the setting on, the stock function
// (same params, same output) with it off.
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as stockAnimate from 'svelte/animate';
import * as stockTransition from 'svelte/transition';
import appCss from '../app.css?raw';
import { fade, flip, fly, scale, slide } from './motion';

function reduceMotion(on: boolean) {
	vi.stubGlobal('window', {
		matchMedia: (query: string) => ({
			matches: on && query === '(prefers-reduced-motion: reduce)'
		})
	});
}

/** A node that fails the test if anything reads it: the reduced path must not measure. */
const untouchable = new Proxy(
	{},
	{
		get: (_, key) => {
			throw new Error(`read node.${String(key)}`);
		}
	}
) as Element;

/** Just enough of a laid-out element for the stock functions to compute from. */
const box = { clientWidth: 200, clientHeight: 40, currentCSSZoom: 1 } as unknown as Element;
const boxStyle = {
	display: 'block',
	opacity: '1',
	transform: 'none',
	transformOrigin: '100px 20px',
	height: '40px',
	width: '200px',
	paddingTop: '4px',
	paddingBottom: '4px',
	marginTop: '0px',
	marginBottom: '0px',
	borderTopWidth: '1px',
	borderBottomWidth: '1px'
};

const from = { left: 0, right: 200, top: 0, bottom: 40, width: 200, height: 40 } as DOMRect;
const to = { left: 0, right: 200, top: 80, bottom: 120, width: 200, height: 40 } as DOMRect;

const transitions = [
	['fade', fade, stockTransition.fade, { duration: 140 }],
	['fly', fly, stockTransition.fly, { y: 28, duration: 220 }],
	['slide', slide, stockTransition.slide, { duration: 160 }],
	['scale', scale, stockTransition.scale, { start: 0.9, duration: 120 }]
] as const;

afterEach(() => vi.unstubAllGlobals());

describe('with Reduce Motion on', () => {
	it.each(transitions)('%s is instant and never measures the node', (_, ours, __, params) => {
		reduceMotion(true);
		vi.stubGlobal('getComputedStyle', () => {
			throw new Error('measured the node');
		});
		expect(ours(untouchable, params)).toEqual({ duration: 0 });
		expect(ours(untouchable)).toEqual({ duration: 0 });
	});

	it('flip is instant and never measures the node', () => {
		reduceMotion(true);
		vi.stubGlobal('getComputedStyle', () => {
			throw new Error('measured the node');
		});
		expect(flip(untouchable, { from, to }, { duration: 200 })).toEqual({ duration: 0 });
	});
});

describe('with Reduce Motion off', () => {
	it.each(transitions)('%s is the stock transition', (_, ours, stock, params) => {
		reduceMotion(false);
		vi.stubGlobal('getComputedStyle', () => boxStyle);
		const got = ours(box, params);
		const want = stock(box, params);
		expect(got.duration).toBe(params.duration);
		expect(got.easing).toBe(want.easing);
		expect(got.css?.(0.25, 0.75)).toBe(want.css?.(0.25, 0.75));
	});

	it('flip is the stock animation', () => {
		reduceMotion(false);
		vi.stubGlobal('getComputedStyle', () => boxStyle);
		const got = flip(box, { from, to });
		const want = stockAnimate.flip(box, { from, to });
		expect(got.duration).toBeGreaterThan(0);
		expect(got.duration).toBe(want.duration);
		expect(got.css?.(0.5, 0.5)).toBe(want.css?.(0.5, 0.5));
		expect(flip(box, { from, to }, { duration: 90 }).duration).toBe(90);
	});

	it('animates when matchMedia is missing or there is no window', () => {
		vi.stubGlobal('getComputedStyle', () => boxStyle);
		vi.stubGlobal('window', {});
		expect(fly(box, { y: 12, duration: 180 }).duration).toBe(180);
		vi.stubGlobal('window', undefined);
		expect(fade(box, { duration: 140 }).duration).toBe(140);
	});
});

describe('app.css', () => {
	it('keeps a single reduced-motion block', () => {
		expect(appCss.match(/@media\s*\(prefers-reduced-motion:\s*reduce\)/g)).toHaveLength(1);
	});
});
