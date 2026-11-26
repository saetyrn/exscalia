// "Today" is a local calendar day. The trap is toISOString(), which reads the
// UTC date: after 8pm ET it is already tomorrow, so every task due today
// looked overdue each evening. System times are built from local components,
// so each assertion holds in whatever zone the runner is in.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { localToday } from './dates';

const fromGetters = (d: Date) =>
	`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

describe('localToday', () => {
	afterEach(() => {
		vi.useRealTimers();
	});

	it('is the local date late in the evening', () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date(2026, 8, 21, 23, 30));
		expect(localToday()).toBe('2026-09-21');
	});

	it('follows the local getters, not the UTC date, on both sides of midnight', () => {
		vi.useFakeTimers();
		// West of UTC the evening times have a UTC date of tomorrow; east of it
		// the early-morning ones have a UTC date of yesterday.
		for (const hour of [0, 1, 19, 20, 21, 23]) {
			const now = new Date(2026, 8, 21, hour, 30);
			vi.setSystemTime(now);
			expect(localToday()).toBe(fromGetters(now));
			expect(localToday()).toBe('2026-09-21');
		}
	});
});
