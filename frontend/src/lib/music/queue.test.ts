import { describe, expect, it } from 'vitest';
import { nextAfterEnd, nextByHand, previous, shuffleRest } from './queue';

describe('music queue', () => {
	it('stops at the end unless repeating all', () => {
		expect(nextAfterEnd(0, 3, 'off')).toBe(1);
		expect(nextAfterEnd(2, 3, 'off')).toBeNull();
		expect(nextAfterEnd(2, 3, 'all')).toBe(0);
		expect(nextAfterEnd(0, 0, 'all')).toBeNull();
	});

	it('next by hand always moves, wrapping', () => {
		expect(nextByHand(2, 3)).toBe(0);
		expect(nextByHand(0, 3)).toBe(1);
	});

	it('previous restarts after three seconds, else steps back', () => {
		expect(previous(2, 3, 10, 'off')).toEqual({ index: 2, restart: true });
		expect(previous(2, 3, 1, 'off')).toEqual({ index: 1, restart: false });
		expect(previous(0, 3, 1, 'off')).toEqual({ index: 0, restart: true });
		expect(previous(0, 3, 1, 'all')).toEqual({ index: 2, restart: false });
	});

	it('shuffle keeps everything up to the current song in place', () => {
		const items = ['a', 'b', 'c', 'd', 'e'];
		let n = 0;
		const out = shuffleRest(items, 1, () => [0.9, 0.1, 0.5][n++ % 3]);
		expect(out.slice(0, 2)).toEqual(['a', 'b']);
		expect([...out].sort()).toEqual(items);
	});
});
