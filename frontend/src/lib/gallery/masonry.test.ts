import { describe, expect, it } from 'vitest';
import { columnCount, placeColumns, tileRatio } from './masonry';

describe('gallery masonry', () => {
	it('fits columns to the width, two at least and eight at most', () => {
		expect(columnCount(360)).toBe(2);
		expect(columnCount(0)).toBe(2);
		expect(columnCount(1200)).toBe(5);
		expect(columnCount(5000)).toBe(8);
	});

	it('clamps a tall strip and squares a picture with no size', () => {
		expect(tileRatio({ width: 1200, height: 3400 })).toBe(2.6);
		expect(tileRatio({ width: null, height: null })).toBe(1);
		expect(tileRatio({ width: 400, height: 300 })).toBeCloseTo(0.75);
	});

	it('puts each tile in the shortest column so far', () => {
		const tall = { width: 100, height: 250 };
		const flat = { width: 100, height: 50 };
		const cols = placeColumns([tall, flat, flat, flat, flat, flat, flat], 2, 100);
		// The tall one (266 px with its gap) fills column 0; flat ones (66 px)
		// stack in column 1 until it outgrows column 0.
		expect(cols).toEqual([
			[0, 6],
			[1, 2, 3, 4, 5]
		]);
	});

	it('leaves tiles already placed where they were when a page is appended', () => {
		const items = Array.from({ length: 30 }, (_, i) => ({
			width: 100,
			height: 60 + ((i * 37) % 140)
		}));
		const first = placeColumns(items.slice(0, 20), 4);
		const both = placeColumns(items, 4);
		first.forEach((column, c) => expect(both[c].slice(0, column.length)).toEqual(column));
	});

	it('counts a caption under a titled tile', () => {
		const cols = placeColumns(
			[
				{ width: 100, height: 100, title: 'Hope' },
				{ width: 100, height: 100 },
				{ width: 100, height: 100 }
			],
			2,
			100
		);
		expect(cols).toEqual([[0], [1, 2]]);
	});
});
