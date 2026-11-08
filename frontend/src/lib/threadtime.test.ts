// The stamp on a mobile conversation row. What matters is which branch a
// timestamp lands in, so every case is pinned to a fixed local clock and the
// localized halves are compared against the same Intl call the helper makes —
// asserting a literal "7:01 PM" would only test the runner's locale.
import { describe, expect, it } from 'vitest';

import { threadTimeLabel } from './stores.svelte';

// Thursday 27 August 2026, 21:40 local.
const NOW = new Date(2026, 7, 27, 21, 40);

describe('threadTimeLabel', () => {
	it('gives a time for today', () => {
		const when = new Date(2026, 7, 27, 19, 1);
		expect(threadTimeLabel(when.toISOString(), NOW)).toBe(
			when.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
		);
	});

	it('counts the day between local midnights, not by elapsed hours', () => {
		// Two hours and twenty minutes back, but the calendar day turned.
		const justAfterMidnight = new Date(2026, 7, 27, 0, 20);
		expect(threadTimeLabel(justAfterMidnight.toISOString(), NOW)).not.toBe('Yesterday');
		// Two and a half hours forward from that, on the other side of midnight.
		const justBeforeMidnight = new Date(2026, 7, 26, 23, 50);
		expect(threadTimeLabel(justBeforeMidnight.toISOString(), NOW)).toBe('Yesterday');
	});

	it('names yesterday', () => {
		expect(threadTimeLabel(new Date(2026, 7, 26, 9, 0).toISOString(), NOW)).toBe('Yesterday');
	});

	it('uses the weekday for the rest of the week', () => {
		for (const day of [25, 24, 23, 22, 21]) {
			const when = new Date(2026, 7, day, 12, 0);
			expect(threadTimeLabel(when.toISOString(), NOW)).toBe(
				when.toLocaleDateString(undefined, { weekday: 'long' })
			);
		}
	});

	it('falls back to a date a week out', () => {
		const when = new Date(2026, 7, 20, 12, 0);
		expect(threadTimeLabel(when.toISOString(), NOW)).toBe(
			when.toLocaleDateString(undefined, { year: '2-digit', month: 'numeric', day: 'numeric' })
		);
	});

	it('reads a timestamp ahead of the clock as today', () => {
		const when = new Date(2026, 7, 28, 8, 0);
		expect(threadTimeLabel(when.toISOString(), NOW)).toBe(
			when.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
		);
	});

	it('says nothing for a missing or unparseable stamp', () => {
		expect(threadTimeLabel(null, NOW)).toBe('');
		expect(threadTimeLabel('', NOW)).toBe('');
		expect(threadTimeLabel('not a date', NOW)).toBe('');
	});
});
