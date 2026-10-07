import { describe, expect, it } from 'vitest';
import type { MediaItem } from '$lib/api';
import {
	cardProgress,
	mergeDayBookends,
	progressNumber,
	starText,
	trackedAs,
	type LogLike
} from './helpers';

const media = (over: Partial<MediaItem> = {}): MediaItem => ({
	path: 'media/Item.md',
	title: 'Item',
	alias: null,
	media_type: 'tv',
	status: 'active',
	creator: null,
	year: null,
	cover: null,
	rating: null,
	started: null,
	finished: null,
	total: null,
	progress: null,
	modified: null,
	...over
});

const log = (kind: string, at: string, path?: string): LogLike => ({
	at,
	kind,
	value: null,
	text: kind,
	path
});

describe('progressNumber', () => {
	it('reads page, episode and bare counts', () => {
		expect(progressNumber('page 120')).toBe(120);
		expect(progressNumber('episode 4')).toBe(4);
		expect(progressNumber('7')).toBe(7);
	});

	it('is null for anything that is not a count', () => {
		expect(progressNumber(null)).toBeNull();
		expect(progressNumber('')).toBeNull();
		expect(progressNumber('act 2')).toBeNull();
		expect(progressNumber('S02E05')).toBeNull();
	});
});

describe('cardProgress', () => {
	it('shows a season/episode position as is, never over a series-wide total', () => {
		expect(cardProgress(media({ progress: 'S02E05', total: 24 }))).toBe('S02E05');
		expect(cardProgress(media({ progress: 's3e1', total: 36 }))).toBe('S3E1');
		expect(cardProgress(media({ progress: 'S01E02' }))).toBe('S01E02');
	});

	it('keeps the fraction for episode counts', () => {
		expect(cardProgress(media({ progress: 'episode 5', total: 24 }))).toBe('5/24 Watched');
		expect(cardProgress(media({ progress: 'episode 30', total: 24 }))).toBe('24/24 Watched');
		expect(cardProgress(media({ progress: '5' }))).toBe('5 Watched');
	});

	it('counts a finished show as all watched', () => {
		expect(cardProgress(media({ status: 'finished', total: 24 }))).toBe('24/24 Watched');
		expect(cardProgress(media({ status: 'finished', progress: 'S02E12', total: 24 }))).toBe(
			'24/24 Watched'
		);
		expect(cardProgress(media({ status: 'finished', progress: 'S02E12' }))).toBe('Finished');
	});

	it('falls back to the status word', () => {
		expect(cardProgress(media())).toBe('Watching');
		expect(cardProgress(media({ status: 'planned' }))).toBe('Watchlist');
	});

	it('leaves books, movies and games as they were', () => {
		expect(cardProgress(media({ media_type: 'book', progress: 'page 120' }))).toBe('Page 120');
		expect(cardProgress(media({ media_type: 'book' }))).toBeNull();
		expect(cardProgress(media({ media_type: 'book', status: 'finished' }))).toBe('Finished');
		expect(cardProgress(media({ media_type: 'movie', progress: '7' }))).toBeNull();
		expect(cardProgress(media({ media_type: 'game' }))).toBe('In progress');
		expect(cardProgress(media({ media_type: 'game', status: 'dropped' }))).toBe('Dropped');
	});
});

describe('mergeDayBookends', () => {
	it('folds a same-day start into the finish on the same item', () => {
		const started = log('started', '2026-08-21', 'media/A.md');
		const finished = log('finished', '2026-08-21', 'media/A.md');
		const merged = mergeDayBookends([finished, started]);
		expect(merged).toHaveLength(1);
		expect(merged[0].kind).toBe('finished');
		expect(merged[0].bookend).toBe(started);
	});

	it('never pairs across items or days', () => {
		const events = [
			log('finished', '2026-08-21', 'media/A.md'),
			log('started', '2026-08-21', 'media/B.md'),
			log('started', '2026-08-20', 'media/A.md')
		];
		const merged = mergeDayBookends(events);
		expect(merged).toHaveLength(3);
		expect(merged.every((event) => event.bookend === undefined)).toBe(true);
	});
});

describe('starText', () => {
	it('shows the 1-10 rating as five stars with halves', () => {
		expect(starText(7)).toBe('★★★½');
		expect(starText(10)).toBe('★★★★★');
		expect(starText(1)).toBe('½');
		expect(starText('8')).toBe('★★★★');
	});

	it('is empty for no rating', () => {
		expect(starText(0)).toBe('');
		expect(starText(null)).toBe('');
		expect(starText('x')).toBe('');
	});
});

describe('trackedAs', () => {
	const book = media({ path: 'media/dune.md', title: 'Dune', media_type: 'book' });

	it('does not treat a same-titled item of another type as tracked', () => {
		const m = { tracked: true, path: book.path, item: book, plays: 1 };
		expect(trackedAs(m, 'movie')).toBe(false);
		expect(trackedAs(m, 'book')).toBe(true);
	});

	it('needs a tracked match with a path', () => {
		expect(trackedAs({ tracked: false }, 'book')).toBe(false);
		expect(trackedAs({ tracked: true, item: book }, 'book')).toBe(false);
		expect(trackedAs({ tracked: true, path: book.path, item: null }, 'movie')).toBe(true);
	});
});
