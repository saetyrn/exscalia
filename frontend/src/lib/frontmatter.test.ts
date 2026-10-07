// The server rewrites `modified` on every save, so the response always differs
// from what the editor sent. Treating that as a real edit made the note page
// rewrite the document after each autosave; these pin what counts as one.
import { describe, expect, it } from 'vitest';
import { alignManagedStamps, sameExceptManagedStamps, splitFrontmatter } from './frontmatter';
import { mergeThreeWay } from './merge';

const fmA = `---\ntitle: N\ncreated: '2026-01-01T00:00:00Z'\nmodified: '2026-01-01T00:00:00Z'\n---\n\nbody\n`;
const fmB = `---\ntitle: N\ncreated: '2026-01-01T00:00:00Z'\nmodified: '2026-07-26T12:00:00Z'\n---\n\nbody\n`;
const fmC = `---\ntitle: N\ncreated: '2026-01-01T00:00:00Z'\nmodified: '2026-07-26T12:00:00Z'\n---\n\nbody changed\n`;
const fmD = `---\ntitle: RENAMED\ncreated: '2026-01-01T00:00:00Z'\nmodified: '2026-01-01T00:00:00Z'\n---\n\nbody\n`;

describe('sameExceptManagedStamps', () => {
	it('a stamp-only change is not a real change', () => {
		expect(sameExceptManagedStamps(fmA, fmB)).toBe(true);
	});

	it('a body change is real', () => {
		expect(sameExceptManagedStamps(fmA, fmC)).toBe(false);
	});

	it('a title change is real', () => {
		expect(sameExceptManagedStamps(fmA, fmD)).toBe(false);
	});

	it('still compares text with no frontmatter', () => {
		expect(sameExceptManagedStamps('x', 'x')).toBe(true);
	});
});

describe('alignManagedStamps', () => {
	it('copies the stamps over', () => {
		expect(alignManagedStamps(fmB, fmA)).toBe(fmA);
	});

	it('leaves other keys alone', () => {
		expect(alignManagedStamps(fmD, fmA)).toContain('RENAMED');
	});

	it('is a no-op without frontmatter', () => {
		expect(alignManagedStamps('plain', fmA)).toBe('plain');
	});
});

// The whole point: an editor lagging by one stamp must not read as an edit.
// Base carries the new stamp; "mine" is the editor's copy, still on the old
// stamp, with an edit on one line; "theirs" edits a different line.
describe('stamp alignment before a three-way merge', () => {
	const twoLineBase = `---\ntitle: N\nmodified: '2026-07-26T12:00:00Z'\n---\n\nalpha\nbeta\n`;
	const staleMine = `---\ntitle: N\nmodified: '2026-01-01T00:00:00Z'\n---\n\nalpha EDITED\nbeta\n`;
	const stampTheirs = `---\ntitle: N\nmodified: '2026-07-26T12:00:00Z'\n---\n\nalpha\nbeta EDITED\n`;
	// Three different stamps — the editor lagging while the server advanced
	// twice — is the case that genuinely collides over a value nobody typed.
	const thirdStamp = `---\ntitle: N\nmodified: '2026-07-26T18:00:00Z'\n---\n\nalpha\nbeta EDITED\n`;

	it('three differing stamps collide without alignment', () => {
		expect(mergeThreeWay(twoLineBase, staleMine, thirdStamp).ok).toBe(false);
	});

	it('alignment rescues that case', () => {
		const aligned = alignManagedStamps(staleMine, twoLineBase);
		expect(mergeThreeWay(twoLineBase, aligned, thirdStamp).ok).toBe(true);
	});

	it('aligning the stamp removes the false conflict', () => {
		const aligned = alignManagedStamps(staleMine, twoLineBase);
		expect(mergeThreeWay(twoLineBase, aligned, stampTheirs)).toEqual({
			ok: true,
			text: `---\ntitle: N\nmodified: '2026-07-26T12:00:00Z'\n---\n\nalpha EDITED\nbeta EDITED\n`
		});
	});
});

// splitFrontmatter takes the first `\n---` after the opening fence as the
// close, and treats a block with no close as body. These pin how the two
// stamp helpers follow that.
describe('fence edge cases', () => {
	it('a `---` rule in the body stays body', () => {
		const ruled = `---\ntitle: N\nmodified: '2026-01-01T00:00:00Z'\n---\n\nabove\n\n---\n\nbelow\n`;
		expect(splitFrontmatter(ruled).body).toBe('\nabove\n\n---\n\nbelow\n');
		const restamped = ruled.replace('2026-01-01', '2026-07-26');
		expect(sameExceptManagedStamps(ruled, restamped)).toBe(true);
		expect(sameExceptManagedStamps(ruled, ruled.replace('below', 'BELOW'))).toBe(false);
	});

	it('a stamp-shaped line after a body rule is content, not a stamp', () => {
		const head = `---\ntitle: N\nmodified: '2026-01-01T00:00:00Z'\n---\n\ntext\n\n---\n`;
		const mine = `${head}modified: by hand\n`;
		const edited = `${head}modified: changed by hand\n`;
		expect(sameExceptManagedStamps(mine, edited)).toBe(false);
		const aligned = alignManagedStamps(mine, fmB);
		expect(aligned).toContain(`modified: '2026-07-26T12:00:00Z'\n---\n\ntext`);
		expect(aligned.endsWith('---\nmodified: by hand\n')).toBe(true);
	});

	it('a block with no closing fence is all body', () => {
		const open = `---\ntitle: N\nmodified: '2026-01-01T00:00:00Z'\n\nbody\n`;
		expect(splitFrontmatter(open)).toEqual({ frontmatter: null, body: open });
		// Its stamp line is ordinary text, so a different stamp is a real change.
		const restamped = open.replace('2026-01-01', '2026-07-26');
		expect(sameExceptManagedStamps(open, restamped)).toBe(false);
		expect(alignManagedStamps(open, fmB)).toBe(open);
		expect(alignManagedStamps(fmA, open)).toBe(fmA);
	});

	it('empty frontmatter', () => {
		const empty = '---\n---\n\nbody\n';
		expect(splitFrontmatter(empty)).toEqual({ frontmatter: '---\n---\n', body: '\nbody\n' });
		expect(sameExceptManagedStamps(empty, '---\n---\n\nbody changed\n')).toBe(false);
		// The server stamping a note on its first save is not an edit.
		const stamped = `---\nmodified: '2026-07-26T12:00:00Z'\n---\n\nbody\n`;
		expect(sameExceptManagedStamps(empty, stamped)).toBe(true);
		// Alignment only replaces stamp lines; it never adds or removes one.
		expect(alignManagedStamps(empty, stamped)).toBe(empty);
		expect(alignManagedStamps(stamped, empty)).toBe(stamped);
	});
});
