import { describe, expect, it } from 'vitest';
import {
	buildLogRequest,
	suggestedProgress,
	todayISO,
	unwatchlistStatus,
	type LogDraft,
	type LogEventLite
} from './mediaactions';

const event = (kind: string, value: string | null = null): LogEventLite => ({
	at: '2026-08-21',
	kind: kind as LogEventLite['kind'],
	value,
	text: value ?? kind
});

const draft = (over: Partial<LogDraft> = {}): LogDraft => ({
	value: '',
	note: '',
	finished: false,
	rating: null,
	date: '2026-08-21',
	...over
});

describe('todayISO', () => {
	it('uses local calendar fields, not UTC', () => {
		// 23:30 on the 21st in a +02:00 zone is the 22nd in UTC.
		const late = new Date(2026, 7, 21, 23, 30);
		expect(todayISO(late)).toBe('2026-08-21');
	});
});

describe('suggestedProgress', () => {
	it('prefills a book with the page it is on', () => {
		expect(suggestedProgress('book', { progress: 'page 120' }, [])).toBe('120');
	});

	it('leaves a fresh book empty', () => {
		expect(suggestedProgress('book', { progress: null }, [])).toBe('');
	});

	it('advances a show to the next episode', () => {
		expect(suggestedProgress('tv', { progress: 'S02E05' }, [])).toBe('S02E06');
	});

	it('advances bare episode numbers too', () => {
		expect(suggestedProgress('tv', { progress: 'episode 4' }, [])).toBe('5');
	});

	it('starts an unlogged show at the pilot', () => {
		expect(suggestedProgress('tv', { progress: null }, [])).toBe('S01E01');
	});

	it('falls back to the log when the item carries no projection', () => {
		expect(suggestedProgress('tv', null, [event('progress', 'S01E09')])).toBe('S01E10');
	});

	it('has nothing to suggest for films and games', () => {
		expect(suggestedProgress('movie', { progress: null }, [])).toBe('');
		expect(suggestedProgress('game', { progress: null }, [])).toBe('');
	});
});

describe('buildLogRequest', () => {
	it('logs a film as a watch, rating included', () => {
		const out = buildLogRequest('movie', draft({ rating: 7 }), '2026-08-21');
		expect(out).toEqual({ body: { kind: 'watched', date: undefined, rating: 7 } });
	});

	it('sends a book page as progress', () => {
		expect(buildLogRequest('book', draft({ value: ' 240 ' }), '2026-08-21')).toEqual({
			body: { kind: 'progress', value: '240', date: undefined }
		});
	});

	it('rejects a page that is not a page', () => {
		expect(buildLogRequest('book', draft({ value: 'half' }), '2026-08-21')).toEqual({
			error: 'Enter your current page.'
		});
	});

	it('accepts episodes as SxxEyy or a bare number', () => {
		expect(buildLogRequest('tv', draft({ value: 'S02E05' }), '2026-08-21')).toEqual({
			body: { kind: 'progress', value: 'S02E05', date: undefined }
		});
		expect(buildLogRequest('tv', draft({ value: '12' }), '2026-08-21')).toEqual({
			body: { kind: 'progress', value: '12', date: undefined }
		});
		expect(buildLogRequest('tv', draft({ value: 'later' }), '2026-08-21')).toEqual({
			error: 'Enter an episode like S02E05 (or a number).'
		});
	});

	it('needs words for a game session', () => {
		expect(buildLogRequest('game', draft({ note: '  ' }), '2026-08-21')).toEqual({
			error: 'Write a short note about the session.'
		});
		expect(buildLogRequest('game', draft({ note: 'Cleared the keep' }), '2026-08-21')).toEqual({
			body: { kind: 'note', text: 'Cleared the keep', date: undefined }
		});
	});

	it('turns the Finished toggle into one finished entry carrying the rating', () => {
		expect(
			buildLogRequest('book', draft({ value: '300', finished: true, rating: 7 }), '2026-08-21')
		).toEqual({ body: { kind: 'finished', date: undefined, rating: 7 } });
	});

	it('omits an untouched date and sends one the user changed', () => {
		const same = buildLogRequest('movie', draft({ date: '2026-08-21' }), '2026-08-21');
		expect(same).toEqual({ body: { kind: 'watched', date: undefined, rating: undefined } });
		const moved = buildLogRequest('movie', draft({ date: '2026-08-14' }), '2026-08-21');
		expect(moved).toEqual({ body: { kind: 'watched', date: '2026-08-14', rating: undefined } });
	});
});

describe('unwatchlistStatus', () => {
	it('restores what the item was before it went on the list', () => {
		expect(unwatchlistStatus({ started: null, progress: null }, [], 'active')).toBe('active');
	});

	it('never restores finished — that would invent a log line', () => {
		expect(unwatchlistStatus({ started: null, progress: null }, [], 'finished')).toBe('dropped');
	});

	it('sends anything already underway back to active', () => {
		expect(unwatchlistStatus({ started: '2026-01-02', progress: null }, [])).toBe('active');
		expect(unwatchlistStatus({ started: null, progress: 'page 40' }, [])).toBe('active');
		expect(unwatchlistStatus({ started: null, progress: null }, [event('started')])).toBe('active');
	});

	it('drops something that was only ever planned', () => {
		expect(unwatchlistStatus({ started: null, progress: null }, [])).toBe('dropped');
		expect(unwatchlistStatus(null, [event('note', null)])).toBe('dropped');
	});
});
