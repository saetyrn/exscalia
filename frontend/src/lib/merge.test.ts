// The three-way merge decides what survives when two devices edit the same
// note — the single most consequential pure function in the client.
import { describe, expect, it } from 'vitest';
import { mergeThreeWay } from './merge';

const BASE = ['# Notes', '', 'alpha line', 'beta line', 'gamma line', ''].join('\n');

describe('mergeThreeWay', () => {
	it('returns mine untouched when both sides made the same edit', () => {
		const both = BASE.replace('alpha', 'ALPHA');
		expect(mergeThreeWay(BASE, both, both)).toEqual({ ok: true, text: both });
	});

	it('takes theirs wholesale when I changed nothing', () => {
		const theirs = BASE.replace('beta', 'BETA');
		expect(mergeThreeWay(BASE, BASE, theirs)).toEqual({ ok: true, text: theirs });
	});

	it('takes mine wholesale when they changed nothing', () => {
		const mine = BASE.replace('gamma', 'GAMMA');
		expect(mergeThreeWay(BASE, mine, BASE)).toEqual({ ok: true, text: mine });
	});

	it('merges edits to different lines', () => {
		const mine = BASE.replace('alpha line', 'alpha line, edited here');
		const theirs = BASE.replace('gamma line', 'gamma line, edited there');
		const result = mergeThreeWay(BASE, mine, theirs);
		expect(result.ok).toBe(true);
		if (result.ok) {
			expect(result.text).toContain('edited here');
			expect(result.text).toContain('edited there');
		}
	});

	it('merges edits to different words of the same sentence', () => {
		const base = 'the quick brown fox jumps over the lazy dog';
		const mine = base.replace('quick', 'sluggish');
		const theirs = base.replace('lazy', 'alert');
		expect(mergeThreeWay(base, mine, theirs)).toEqual({
			ok: true,
			text: 'the sluggish brown fox jumps over the alert dog'
		});
	});

	it('reports a conflict when both sides rewrote the same ground', () => {
		const mine = BASE.replace('beta line', 'my beta rewrite');
		const theirs = BASE.replace('beta line', 'their beta rewrite');
		const result = mergeThreeWay(BASE, mine, theirs);
		expect(result.ok).toBe(false);
		if (!result.ok) expect(result.conflicts).toBeGreaterThan(0);
	});

	it('keeps one copy of an identical insertion made at the same point', () => {
		const insert = BASE.replace('beta line', 'beta line\ninserted twice');
		const result = mergeThreeWay(BASE, insert, insert);
		expect(result).toEqual({ ok: true, text: insert });
		if (result.ok) {
			expect(result.text.match(/inserted twice/g)).toHaveLength(1);
		}
	});

	it('conflicts on different insertions at the same point (order would be arbitrary)', () => {
		const mine = BASE.replace('beta line', 'beta line\nmine first');
		const theirs = BASE.replace('beta line', 'beta line\ntheirs first');
		expect(mergeThreeWay(BASE, mine, theirs).ok).toBe(false);
	});

	it('merges a deletion against a distant edit', () => {
		const mine = BASE.replace('alpha line\n', '');
		const theirs = BASE.replace('gamma', 'GAMMA');
		const result = mergeThreeWay(BASE, mine, theirs);
		expect(result.ok).toBe(true);
		if (result.ok) {
			expect(result.text).not.toContain('alpha');
			expect(result.text).toContain('GAMMA');
		}
	});

	it('conflicts when they edit a line I deleted', () => {
		const mine = BASE.replace('beta line\n', '');
		const theirs = BASE.replace('beta line', 'beta line polished');
		expect(mergeThreeWay(BASE, mine, theirs).ok).toBe(false);
	});

	it('handles append vs prepend without losing either', () => {
		const mine = `intro added\n${BASE}`;
		const theirs = `${BASE}outro added\n`;
		const result = mergeThreeWay(BASE, mine, theirs);
		expect(result.ok).toBe(true);
		if (result.ok) {
			expect(result.text.startsWith('intro added\n')).toBe(true);
			expect(result.text.endsWith('outro added\n')).toBe(true);
		}
	});

	it('survives an empty base (both sides wrote a new note)', () => {
		expect(mergeThreeWay('', 'same text', 'same text')).toEqual({
			ok: true,
			text: 'same text'
		});
		expect(mergeThreeWay('', 'one version', 'another version').ok).toBe(false);
	});
});
