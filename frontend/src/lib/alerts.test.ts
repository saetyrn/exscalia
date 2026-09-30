import { describe, expect, it } from 'vitest';
import { unshownAlerts } from './alerts';

describe('unshownAlerts', () => {
	it('returns an alert once even when a later poll delivers it again', () => {
		const lecture = { key: 'e1|2026-09-23T16:00|15', title: 'Lecture' };
		expect(unshownAlerts([lecture])).toEqual([lecture]);
		// Unacked (or acked after this poll started): the server re-sends it.
		expect(unshownAlerts([lecture])).toEqual([]);
	});

	it('passes new keys through alongside ones already shown', () => {
		const first = { key: 'task:a:2026-09-23T09:00', title: 'Pay rent' };
		const second = { key: 'task:b:2026-09-23T09:00', title: 'Call dentist' };
		unshownAlerts([first]);
		expect(unshownAlerts([first, second])).toEqual([second]);
	});

	it('collapses a key repeated within one response', () => {
		const alert = { key: 'omnia:abc:2026-09-23', title: 'Nudge' };
		expect(unshownAlerts([alert, alert])).toEqual([alert]);
	});
});
