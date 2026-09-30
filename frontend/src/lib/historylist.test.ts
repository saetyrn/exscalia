// The version-history list. The rules pinned down here are the ones paging
// exercises without anyone looking: an older page never repeats a row when a
// save lands between fetches, a size change is only claimed against a loaded
// neighbour, and day headers follow the browser's calendar, not the server's.
import { describe, expect, it } from 'vitest';
import { appendOlder, dayBreak, sizeDelta } from './historylist';

// Built from local wall-clock parts so the expectations hold in any time zone.
const at = (day: number, hour: number, minute = 0) =>
	new Date(2026, 8, day, hour, minute).toISOString();

const row = (sha: string, size: number, committed_at = at(22, 12), deleted = false) => ({
	sha,
	size,
	committed_at,
	deleted
});

describe('appendOlder', () => {
	it('appends the next page in order', () => {
		const shas = appendOlder([row('c', 3), row('b', 2)], [row('a', 1)]).map((v) => v.sha);
		expect(shas).toEqual(['c', 'b', 'a']);
	});

	it('drops rows a shifted window repeats', () => {
		// One autosave landed after the first page: the second page starts
		// with the first page's last row.
		const shas = appendOlder([row('d', 4), row('c', 3)], [row('c', 3), row('b', 2)]).map(
			(v) => v.sha
		);
		expect(shas).toEqual(['d', 'c', 'b']);
	});
});

describe('sizeDelta', () => {
	const list = [row('d', 1400), row('c', 1088), row('b', 1133), row('a', 0, at(22, 9), true)];

	it('signs the change against the next-older version', () => {
		expect(sizeDelta(list, 0)).toBe('+312 B');
		expect(sizeDelta(list, 1)).toBe('−45 B');
	});

	it('has nothing to say beside an absent note or past the loaded rows', () => {
		expect(sizeDelta(list, 2)).toBeNull();
		expect(sizeDelta(list, 3)).toBeNull();
		expect(sizeDelta([row('b', 5)], 0)).toBeNull();
	});

	it('switches to KB and shows an unchanged size bare', () => {
		expect(sizeDelta([row('b', 4096), row('a', 1024)], 0)).toBe('+3.0 KB');
		expect(sizeDelta([row('b', 70), row('a', 70)], 0)).toBe('0 B');
	});
});

describe('dayBreak', () => {
	it('heads the first row and each new local day only', () => {
		const list = [
			row('d', 1, at(23, 0, 15)),
			row('c', 1, at(22, 23, 30)),
			row('b', 1, at(22, 0, 5)),
			row('a', 1, at(21, 23, 59))
		];
		expect(list.map((_, i) => dayBreak(list, i) !== null)).toEqual([true, true, false, true]);
	});
});
