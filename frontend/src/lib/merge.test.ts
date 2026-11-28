// The three-way merge decides what survives when two devices edit the same
// note — the single most consequential pure function in the client.
import { describe, expect, it } from 'vitest';
import { alignManagedStamps } from './frontmatter';
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

	it('keeps one copy of an identical insertion when the sides otherwise differ', () => {
		// The case above returns early (mine === theirs); this one reaches the
		// replay loop, where the duplicate has to be skipped.
		const insert = BASE.replace('beta line', 'beta line, extended');
		const mine = insert.replace('alpha', 'ALPHA');
		expect(mergeThreeWay(BASE, mine, insert)).toEqual({ ok: true, text: mine });
	});

	it('merges the same replacement made on both sides when one side also edited elsewhere', () => {
		// A save that landed, seen again from a draft or a retry: the shared
		// rewrite agrees with itself and must not read as contention.
		const both = BASE.replace('beta line', 'beta rewritten');
		const theirs = both.replace('gamma', 'GAMMA');
		expect(mergeThreeWay(BASE, both, theirs)).toEqual({ ok: true, text: theirs });
	});

	it('merges the same deletion made on both sides when one side also edited elsewhere', () => {
		const both = BASE.replace('gamma line\n', '');
		const mine = both.replace('# Notes', '# Field notes');
		expect(mergeThreeWay(BASE, mine, both)).toEqual({ ok: true, text: mine });
	});

	it('still conflicts when one side extends a replacement the other made', () => {
		const mine = BASE.replace('beta line', 'beta rewritten');
		const theirs = BASE.replace('beta line', 'beta rewritten twice').replace('gamma', 'GAMMA');
		expect(mergeThreeWay(BASE, mine, theirs).ok).toBe(false);
	});

	// Independent edits that only meet at one point. Treating every touch as
	// contention turned each of these into a 'pick one' banner or a conflict
	// copy.
	it('merges a deleted list item against an item added after it', () => {
		const base = '- one\n- two\n- three\n';
		const mine = '- one\n- three\n';
		const theirs = '- one\n- two\n- two and a half\n- three\n';
		expect(mergeThreeWay(base, mine, theirs)).toEqual({
			ok: true,
			text: '- one\n- two and a half\n- three\n'
		});
	});

	it('merges a deleted line against an edit to the next line', () => {
		expect(mergeThreeWay('a\nb\nc\n', 'a\nc\n', 'a\nb\nC\n')).toEqual({
			ok: true,
			text: 'a\nC\n'
		});
	});

	it('merges a word inserted before a word the other side capitalised', () => {
		expect(mergeThreeWay('Hello world', 'Hello brave world', 'Hello World')).toEqual({
			ok: true,
			text: 'Hello brave World'
		});
	});

	it('merges rewrites of neighbouring words', () => {
		const base = 'the cat sat on the mat';
		expect(mergeThreeWay(base, 'the dog sat on the mat', 'the cat stood on the mat')).toEqual({
			ok: true,
			text: 'the dog stood on the mat'
		});
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

// What the note page's save loop hands the merge (fe-editor#1). The page has no
// harness of its own, so these pin the two inputs it must get right: `mine` is
// the LIVE document, and the base is the server's text at serverRevision.
describe('save-loop merge inputs', () => {
	const note = (modified: string, body: string) =>
		['---', 'created: 2026-09-01T10:00:00', `modified: ${modified}`, '---', body].join('\n');

	it('keeps text typed while the rejected PUT was in flight', () => {
		const base = note('2026-09-22T10:00:00', '# Plan\n\nShip v2.\n');
		// The PUT carried "Write docs."; "And tests." was typed during it.
		const live = note('2026-09-22T10:00:00', '# Plan\n\nShip v2. Write docs. And tests.\n');
		const theirs = note('2026-09-22T10:00:05', '# Release plan\n\nShip v2.\n');
		const result = mergeThreeWay(base, alignManagedStamps(live, base), theirs);
		expect(result).toEqual({
			ok: true,
			text: note('2026-09-22T10:00:05', '# Release plan\n\nShip v2. Write docs. And tests.\n')
		});
	});

	it('keeps a remote edit that pullRemote merged in while the PUT was in flight', () => {
		// pullRemote already moved the base to their text, so base === theirs.
		const theirs = note('2026-09-22T10:00:05', '# Release plan\n\nShip v2.\n');
		const live = note('2026-09-22T10:00:00', '# Release plan\n\nShip v2. Write docs.\n');
		const result = mergeThreeWay(theirs, alignManagedStamps(live, theirs), theirs);
		expect(result).toEqual({
			ok: true,
			text: note('2026-09-22T10:00:05', '# Release plan\n\nShip v2. Write docs.\n')
		});
	});

	it('merges typing through a save cleanly against the saved text as base', () => {
		// "alpha" was saved (the server now holds it, stamped); "beta" was typed
		// during that PUT; another device then changed the last line. With the
		// pre-save text as base, both sides insert at the same point and the
		// user is asked to pick one.
		const saved = note('2026-09-22T10:00:01', 'intro\nnotes alpha\nlast line\n');
		const live = note('2026-09-22T10:00:00', 'intro\nnotes alphabeta\nlast line\n');
		const theirs = note('2026-09-22T10:00:09', 'intro\nnotes alpha\nlast line, edited\n');
		const result = mergeThreeWay(saved, alignManagedStamps(live, saved), theirs);
		expect(result).toEqual({
			ok: true,
			text: note('2026-09-22T10:00:09', 'intro\nnotes alphabeta\nlast line, edited\n')
		});
	});
});
