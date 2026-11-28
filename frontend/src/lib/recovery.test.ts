// A draft left on one device must never be put back over edits another device
// saved since (fe-editor#4): recovery merges from the draft's base, and only
// asks when both sides rewrote the same lines.
import { describe, expect, it } from 'vitest';
import type { LocalDraft } from './drafts';
import { reconcileDraft } from './recovery';

const BASE = ['# Trip', '', 'pack the tent', 'book the train', 'call the hut', ''].join('\n');

const draft = (content: string, extra: Partial<LocalDraft> = {}): LocalDraft => ({
	path: 'trip.md',
	owner: 'tab-a',
	content,
	baseRevision: 'r1',
	baseContent: BASE,
	updatedAt: 1,
	...extra
});

const stamped = (modified: string, body: string) =>
	['---', 'created: 2026-09-01T10:00:00', `modified: ${modified}`, '---', body].join('\n');

describe('reconcileDraft', () => {
	it('has nothing to recover without a draft', () => {
		expect(reconcileDraft(null, { content: BASE, revision: 'r1' })).toEqual({ kind: 'none' });
	});

	it('treats a draft that differs only by the save stamp as saved', () => {
		const saved = stamped('2026-09-22T10:00:00', BASE);
		const server = stamped('2026-09-22T10:00:02', BASE);
		expect(
			reconcileDraft(draft(saved, { baseContent: saved, baseRevision: 'r0' }), {
				content: server,
				revision: 'r1'
			})
		).toEqual({ kind: 'none' });
	});

	it('recovers the draft as written when the server is still on its base', () => {
		const mine = BASE.replace('pack the tent', 'pack the tent and stove');
		expect(reconcileDraft(draft(mine), { content: BASE, revision: 'r1' })).toEqual({
			kind: 'recover',
			doc: mine
		});
	});

	it('merges the draft onto edits the server gained since', () => {
		const mine = BASE.replace('pack the tent', 'pack the tent and stove');
		const theirs = BASE.replace('call the hut', 'call the hut on Friday');
		const result = reconcileDraft(draft(mine), { content: theirs, revision: 'r2' });
		expect(result.kind).toBe('recover');
		if (result.kind === 'recover') {
			expect(result.doc).toContain('pack the tent and stove');
			expect(result.doc).toContain('call the hut on Friday');
		}
	});

	it('merges past a save stamp that lags the draft base', () => {
		const base = stamped('2026-09-22T10:00:02', BASE);
		// The editor did not redraw for the last save's stamp.
		const mine = stamped('2026-09-22T10:00:00', BASE.replace('book the train', 'book the 7:10 train'));
		const theirs = stamped('2026-09-22T11:30:00', BASE.replace('call the hut', 'call the hut twice'));
		const result = reconcileDraft(draft(mine, { baseContent: base }), {
			content: theirs,
			revision: 'r2'
		});
		expect(result).toEqual({
			kind: 'recover',
			doc: stamped(
				'2026-09-22T11:30:00',
				BASE.replace('book the train', 'book the 7:10 train').replace('call the hut', 'call the hut twice')
			)
		});
	});

	it('reports a conflict, with the draft and its base, when both rewrote the same line', () => {
		const mine = BASE.replace('book the train', 'my train plan');
		const theirs = BASE.replace('book the train', 'their train plan');
		expect(reconcileDraft(draft(mine), { content: theirs, revision: 'r2' })).toEqual({
			kind: 'conflict',
			doc: mine,
			base: { content: BASE, revision: 'r1' }
		});
	});

	it('has nothing to recover when the server already holds everything the draft added', () => {
		// A keepalive PUT that landed after the tab died, then an edit elsewhere.
		const mine = `${BASE}bring cards\n`;
		const theirs = `${BASE.replace('# Trip', '# Trip north')}bring cards\n`;
		expect(reconcileDraft(draft(mine), { content: theirs, revision: 'r2' })).toEqual({
			kind: 'none'
		});
	});

	it('has nothing to recover when the draft rewrote a line the server already saved', () => {
		// The same keepalive case, but the draft replaced text instead of
		// appending: both sides carry the identical rewrite, which is not a
		// conflict with the other device's later edit.
		const mine = BASE.replace('pack the tent', 'pack the stove');
		const theirs = mine.replace('call the hut', 'call the hut on Friday');
		expect(reconcileDraft(draft(mine), { content: theirs, revision: 'r2' })).toEqual({
			kind: 'none'
		});
	});

	it('recovers the rest of a draft whose rewrite already reached the server', () => {
		const saved = BASE.replace('pack the tent', 'pack the stove');
		const mine = `${saved}bring cards\n`;
		const theirs = saved.replace('call the hut', 'call the hut on Friday');
		expect(reconcileDraft(draft(mine), { content: theirs, revision: 'r2' })).toEqual({
			kind: 'recover',
			doc: `${theirs}bring cards\n`
		});
	});

	it('still merges from an empty base', () => {
		const result = reconcileDraft(draft('first words', { baseContent: '' }), {
			content: 'other words',
			revision: 'r2'
		});
		expect(result.kind).toBe('conflict');
	});

	it('recovers a draft from before drafts carried their base as written', () => {
		const legacy = draft(BASE.replace('pack the tent', 'pack the tarp'), {
			owner: '',
			baseRevision: 'r0',
			baseContent: undefined
		});
		expect(reconcileDraft(legacy, { content: BASE, revision: 'r5' })).toEqual({
			kind: 'recover',
			doc: legacy.content
		});
	});
});
