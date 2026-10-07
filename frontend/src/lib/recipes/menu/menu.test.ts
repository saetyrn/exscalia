// The menu's small rules: drags and wheel turns to positions, positions to
// recipes, the focused title's caption, and which images it may show.
import { describe, expect, it } from 'vitest';
import {
	cardMeta,
	counter,
	dragPosition,
	landing,
	localImage,
	optionId,
	pxPerItem,
	renderRange,
	wheelPixels
} from './menu';

describe('recipe menu gestures', () => {
	it('turns one item per item-height of drag, up for the next recipe', () => {
		expect(dragPosition(2, pxPerItem(false), false, 8)).toBe(3);
		expect(dragPosition(2, -pxPerItem(false), false, 8)).toBe(1);
		expect(dragPosition(2, pxPerItem(true), true, 8)).toBe(3);
		expect(dragPosition(2, 30, false, 8)).toBeCloseTo(2.25);
	});

	it('gives with resistance past either end', () => {
		expect(dragPosition(0, -240, false, 8)).toBeCloseTo(-0.7);
		expect(dragPosition(7, 240, false, 8)).toBeCloseTo(7.7);
	});

	it('lands a drag on the nearest recipe, never outside the list', () => {
		expect(landing(2.4, 8)).toBe(2);
		expect(landing(2.6, 8)).toBe(3);
		expect(landing(-0.7, 8)).toBe(0);
		expect(landing(7.7, 8)).toBe(7);
		expect(landing(dragPosition(0, 130, false, 8), 8)).toBe(1);
		expect(landing(dragPosition(0, 50, false, 8), 8)).toBe(0);
		expect(landing(3, 0)).toBe(0);
	});

	it('renders four items each side of focus', () => {
		expect(renderRange(0, 20)).toEqual([0, 4]);
		expect(renderRange(10, 20)).toEqual([6, 14]);
		expect(renderRange(19, 20)).toEqual([15, 19]);
		expect(renderRange(0, 1)).toEqual([0, 0]);
	});

	it('reads wheel deltas in pixels, lines and pages', () => {
		expect(wheelPixels(30, 0)).toBe(30);
		expect(wheelPixels(3, 1)).toBe(48);
		expect(wheelPixels(-1, 2, 600)).toBe(-600);
	});
});

describe('recipe menu captions and images', () => {
	it('captions the focused title with its time and up to three tags', () => {
		expect(cardMeta({ total_minutes: 25, tags: ['pasta', 'quick', 'weeknight', 'italian'] })).toBe(
			'25 min · pasta · quick · weeknight'
		);
		expect(cardMeta({ total_minutes: 90, tags: [] })).toBe('1 h 30 min');
		expect(cardMeta({ total_minutes: null, tags: ['soup'] })).toBe('soup');
		expect(cardMeta({ total_minutes: null, tags: [] })).toBe('');
	});

	it('shows only images the app serves itself', () => {
		expect(localImage('/api/attachments/ab12.jpg')).toBe('/api/attachments/ab12.jpg');
		expect(localImage('https://example.com/pie.jpg')).toBeNull();
		expect(localImage('//example.com/api/attachments/pie.jpg')).toBeNull();
		expect(localImage('data:image/png;base64,AAAA')).toBeNull();
		expect(localImage('')).toBeNull();
		expect(localImage(null)).toBeNull();
	});

	it('makes option ids without spaces', () => {
		expect(optionId('recipes/my pie.md')).toBe('rc-menu-recipes%2Fmy%20pie.md');
		expect(optionId('recipes/my pie.md')).not.toMatch(/\s/);
	});

	it('counts in two digits', () => {
		expect(counter(0, 8)).toBe('01 / 08');
		expect(counter(11, 120)).toBe('12 / 120');
	});
});
