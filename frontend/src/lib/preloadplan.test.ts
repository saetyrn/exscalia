// What the preloader chooses to fetch. The daily-note guard is the one that
// matters most: the app reaches a daily note through a get-or-create POST, so a
// wrong answer here would write a note into the vault just because someone
// opened the app.
import { describe, expect, it } from 'vitest';
import type { MediaHub, MediaItem } from './api';
import type { SaveDataSource } from './preloadplan';
import {
	coverUrls,
	MAX_WARM_COVERS,
	notesToPrefetch,
	saveDataEnabled,
	todaysDailyPath
} from './preloadplan';

const item = (title: string, cover: string | null): MediaItem =>
	({ path: `media/${title}.md`, title, cover }) as MediaItem;

const hub = (over: Partial<MediaHub> = {}): MediaHub =>
	({
		profile: { avatar: '', banner: '' },
		favorites: [],
		recent: [],
		watchlist: [],
		...over
	}) as MediaHub;

describe('saveDataEnabled', () => {
	it('is false when the browser says nothing about the connection', () => {
		expect(saveDataEnabled({} as SaveDataSource)).toBe(false);
		expect(saveDataEnabled({ connection: {} } as SaveDataSource)).toBe(false);
		expect(saveDataEnabled(undefined)).toBe(false);
	});

	it('is true on Data Saver, which is the whole skip condition', () => {
		expect(saveDataEnabled({ connection: { saveData: true } } as SaveDataSource)).toBe(true);
	});

	it('does not read a truthy-but-off value as on', () => {
		expect(saveDataEnabled({ connection: { saveData: false } } as SaveDataSource)).toBe(false);
	});
});

describe('coverUrls', () => {
	it('puts the profile art first — it is the largest thing drawn', () => {
		expect(
			coverUrls(
				hub({
					profile: { avatar: '/a.png', banner: '/b.png' } as MediaHub['profile'],
					favorites: [item('Dune', '/dune.png')]
				})
			)
		).toEqual(['/a.png', '/b.png', '/dune.png']);
	});

	it('warms each URL once, however many sections list it', () => {
		const dune = item('Dune', '/dune.png');
		expect(
			coverUrls(
				hub({
					favorites: [dune],
					watchlist: [dune],
					recent: [{ item: dune }] as MediaHub['recent']
				})
			)
		).toEqual(['/dune.png']);
	});

	it('skips items with no cover at all', () => {
		expect(coverUrls(hub({ favorites: [item('Blank', null)] }))).toEqual([]);
	});

	it('stays bounded so a long watchlist is not a download', () => {
		const many = Array.from({ length: 200 }, (_, i) => item(`m${i}`, `/m${i}.png`));
		expect(coverUrls(hub({ watchlist: many }))).toHaveLength(MAX_WARM_COVERS);
		expect(coverUrls(hub({ watchlist: many }), 5)).toHaveLength(5);
	});
});

describe('todaysDailyPath', () => {
	it('matches the backend layout, zero-padded', () => {
		expect(todaysDailyPath(new Date(2026, 7, 9))).toBe('daily/2026-08-09.md');
		expect(todaysDailyPath(new Date(2026, 11, 31))).toBe('daily/2026-12-31.md');
	});
});

describe('notesToPrefetch', () => {
	const existing = (...paths: string[]) => new Set(paths);

	it('takes the top of the recent list', () => {
		expect(
			notesToPrefetch(
				['a.md', 'b.md', 'c.md', 'd.md'],
				existing('a.md', 'b.md', 'c.md', 'd.md'),
				'daily/2026-08-21.md'
			)
		).toEqual(['a.md', 'b.md', 'c.md']);
	});

	it("adds today's daily when the vault already has it", () => {
		expect(notesToPrefetch(['a.md'], existing('a.md', 'daily/2026-08-21.md'), 'daily/2026-08-21.md')).toEqual([
			'a.md',
			'daily/2026-08-21.md'
		]);
	});

	it("never asks for a daily note the vault does not have", () => {
		// Fetching it is how one gets created. This must stay empty.
		expect(notesToPrefetch([], existing('a.md'), 'daily/2026-08-21.md')).toEqual([]);
	});

	it('drops recent paths the vault no longer lists', () => {
		expect(notesToPrefetch(['renamed.md', 'a.md'], existing('a.md'), 'daily/x.md')).toEqual(['a.md']);
	});

	it('does not list the daily twice when it is also recent', () => {
		const daily = 'daily/2026-08-21.md';
		expect(notesToPrefetch([daily, 'a.md'], existing(daily, 'a.md'), daily)).toEqual([daily, 'a.md']);
	});

	it('is empty when nothing is recent and no daily exists', () => {
		expect(notesToPrefetch([], existing(), 'daily/2026-08-21.md')).toEqual([]);
	});
});
