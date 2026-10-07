import { describe, expect, it } from 'vitest';
import { conflictStamp, pickCurrentIndex } from './paperview';

// A phone-sized reader: 600px tall, starting 100px down the viewport.
const reader = { top: 100, bottom: 700 };

describe('pickCurrentIndex', () => {
	it('makes a last page shorter than the reader current at the bottom', () => {
		// Scrolled all the way down: the last sheet is fully on screen but its
		// top can never reach the reader's, and page 2 still shows its tail.
		const sheets = [
			{ top: -900, bottom: -390 },
			{ top: -390, bottom: 120 },
			{ top: 120, bottom: 630 }
		];
		expect(pickCurrentIndex(reader, sheets, true)).toBe(2);
	});

	it('picks the sheet filling more of the reader', () => {
		const sheets = [
			{ top: -200, bottom: 300 }, // 200px visible
			{ top: 300, bottom: 800 } // 400px visible
		];
		expect(pickCurrentIndex(reader, sheets, false)).toBe(1);
	});

	it('keeps the earlier sheet on a tie', () => {
		const sheets = [
			{ top: -100, bottom: 400 }, // 300px visible
			{ top: 400, bottom: 900 } // 300px visible
		];
		expect(pickCurrentIndex(reader, sheets, false)).toBe(0);
	});

	it('lets the bottom of the scroller win over visibility', () => {
		// A tall page followed by a short one, scrolled to the end: the short
		// last page shows less than the tail of the page before it.
		const sheets = [
			{ top: -260, bottom: 440 }, // 340px visible
			{ top: 440, bottom: 640 } // 200px visible
		];
		expect(pickCurrentIndex(reader, sheets, false)).toBe(0);
		expect(pickCurrentIndex(reader, sheets, true)).toBe(1);
	});

	it('falls back to the first page when there is nothing to measure', () => {
		expect(pickCurrentIndex(reader, [], false)).toBe(0);
		expect(pickCurrentIndex(reader, [], true)).toBe(0);
	});

	it('ignores sheets entirely outside the reader', () => {
		const sheets = [
			{ top: -1400, bottom: -900 },
			{ top: 150, bottom: 650 },
			{ top: 900, bottom: 1400 }
		];
		expect(pickCurrentIndex(reader, sheets, false)).toBe(1);
	});
});

describe('conflictStamp', () => {
	it('reads the UTC stamp the server puts in a conflict id', () => {
		expect(conflictStamp('a1b2.conflict-20260921140559')?.toISOString()).toBe(
			'2026-09-21T14:05:59.000Z'
		);
	});

	it('survives a collision variant with a rewritten last digit', () => {
		// The store's counter replaces the last digit of the seconds.
		expect(conflictStamp('a1b2.conflict-20260921140552')?.toISOString()).toBe(
			'2026-09-21T14:05:52.000Z'
		);
		// Not a real second: keep the minute rather than lose the time.
		expect(conflictStamp('a1b2.conflict-20260921140569')?.toISOString()).toBe(
			'2026-09-21T14:05:00.000Z'
		);
	});

	it('returns null for ids that carry no real time', () => {
		expect(conflictStamp('a1b2')).toBeNull();
		expect(conflictStamp('a1b2.conflict-2026092114')).toBeNull();
		expect(conflictStamp('a1b2.conflict-20261321140559')).toBeNull(); // month 13
		expect(conflictStamp('a1b2.conflict-20260231140559')).toBeNull(); // Feb 31
		expect(conflictStamp('a1b2.conflict-20260921250559')).toBeNull(); // hour 25
	});
});
