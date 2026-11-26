import { describe, expect, it } from 'vitest';
import type { BriefingDay, CalendarEventOccurrence } from '$lib/api';
import { comingUp, eventOver, noteExcerpt, statParts } from './hub';

function event(over: Partial<CalendarEventOccurrence>): CalendarEventOccurrence {
	return {
		id: 'ev',
		event_id: 'ev',
		title: 'Event',
		all_day: false,
		timezone: 'UTC',
		occurrence_date: '2026-10-30',
		occurrence_start: '2026-10-30T13:00:00Z',
		occurrence_end: '2026-10-30T14:00:00Z',
		covers: ['2026-10-30'],
		revision: 'r',
		...over
	};
}

describe('statParts', () => {
	it('lists only what happened', () => {
		expect(
			statParts({
				notes_created: 1,
				notes_modified: 4,
				tasks_completed: 2,
				flashcards_due: 0,
				activity_hours_yesterday: 0.04,
				pending_changes: 0,
				media_in_progress: 0
			})
		).toEqual(['1 note', '2 tasks done']);
		expect(statParts(null)).toEqual([]);
	});
});

describe('noteExcerpt', () => {
	it('skips frontmatter, metadata comments, headings, tags and blank lines', () => {
		const content = [
			'---',
			'created: 2026-10-30',
			'---',
			'#daily',
			'',
			'## Notes',
			'Slept badly.',
			'<!-- task: {"id": "a"} -->',
			'- [ ] Call the bank',
			'Coffee helped.',
			'More',
			'Even more'
		].join('\n');
		expect(noteExcerpt(content)).toEqual(['Slept badly.', '- [ ] Call the bank', 'Coffee helped.', 'More']);
	});

	it('finds nothing in an untouched template', () => {
		expect(noteExcerpt('#daily\n\n## Notes\n\n## Tasks\n\n- [ ]\n')).toEqual([]);
	});
});

describe('comingUp', () => {
	it('lists days after today, events before tasks, without dailies', () => {
		const week: BriefingDay[] = [
			{ date: '2026-10-30', events: [event({ id: 'today' })], tasks: [] },
			{
				date: '2026-10-31',
				events: [event({ id: 'party', title: 'Party', all_day: true })],
				tasks: [
					{ id: 't1', text: 'Problem set' } as BriefingDay['tasks'][number],
					{ id: 't2', text: 'Stretch', repeats: 'daily' } as BriefingDay['tasks'][number]
				]
			}
		];
		const items = comingUp(week, '2026-10-30');
		expect(items.map((i) => [i.kind, i.title, i.time])).toEqual([
			['event', 'Party', ''],
			['task', 'Problem set', '']
		]);
	});
});

describe('eventOver', () => {
	it('is over once the end has passed, and never for an all-day event', () => {
		const now = new Date('2026-10-30T15:00:00Z');
		expect(eventOver(event({}), now)).toBe(true);
		expect(eventOver(event({ occurrence_end: '2026-10-30T16:00:00Z' }), now)).toBe(false);
		expect(eventOver(event({ all_day: true }), now)).toBe(false);
	});
});
