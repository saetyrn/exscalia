import { mergeThreeWay } from '../src/lib/merge';
let pass = 0, fail = 0;
const check = (name: string, got: unknown, want: unknown) => {
	const ok = JSON.stringify(got) === JSON.stringify(want);
	ok ? pass++ : fail++;
	console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}`);
	if (!ok) console.log(`      got  ${JSON.stringify(got)}\n      want ${JSON.stringify(want)}`);
};

const base = '# Trip\n\n## Packing\n- socks\n\n## Budget\n- flights\n';

// Laptop edits Packing, PC edits Budget: disjoint, must merge.
const mine   = '# Trip\n\n## Packing\n- socks\n- boots\n\n## Budget\n- flights\n';
const theirs = '# Trip\n\n## Packing\n- socks\n\n## Budget\n- flights\n- hotel\n';
check('disjoint sections merge', mergeThreeWay(base, mine, theirs),
	{ ok: true, text: '# Trip\n\n## Packing\n- socks\n- boots\n\n## Budget\n- flights\n- hotel\n' });

// Same line, different text: must NOT guess.
check('same line conflicts',
	mergeThreeWay('a\nb\nc\n', 'a\nBBB\nc\n', 'a\nZZZ\nc\n').ok, false);

// One side idle.
check('only theirs changed', mergeThreeWay(base, base, theirs), { ok: true, text: theirs });
check('only mine changed',   mergeThreeWay(base, mine, base),   { ok: true, text: mine });
check('identical edits',     mergeThreeWay(base, mine, mine),   { ok: true, text: mine });

// Same-point insertion, different text -> conflict, not silent interleave.
check('same-point differing inserts conflict',
	mergeThreeWay('ab', 'aXb', 'aYb').ok, false);

// Same-point insertion, identical text -> merged once, not doubled.
check('same-point identical inserts dedupe',
	mergeThreeWay('ab', 'aXb', 'aXb'), { ok: true, text: 'aXb' });

// Character level within one line, different words.
check('different words in one sentence',
	mergeThreeWay('the quick brown fox\n', 'the SLOW brown fox\n', 'the quick brown DOG\n'),
	{ ok: true, text: 'the SLOW brown DOG\n' });

// Deletion on one side, unrelated edit on the other.
check('delete + unrelated edit',
	mergeThreeWay('one\ntwo\nthree\n', 'one\nthree\n', 'one\ntwo\nthree\nfour\n'),
	{ ok: true, text: 'one\nthree\nfour\n' });

// Both delete the same line: same region, identical result.
check('both delete same line',
	mergeThreeWay('one\ntwo\nthree\n', 'one\nthree\n', 'one\nthree\n'),
	{ ok: true, text: 'one\nthree\n' });

// Empty base.
check('append to empty', mergeThreeWay('', 'hi', ''), { ok: true, text: 'hi' });

// --- frontmatter stamps ----------------------------------------------------
// The server rewrites `modified` on every save, so the response always differs
// from what the editor sent. Treating that as a real edit made the note page
// rewrite the document after each autosave.
import { sameExceptManagedStamps, alignManagedStamps } from '../src/lib/frontmatter';

const fmA = `---\ntitle: N\ncreated: '2026-01-01T00:00:00Z'\nmodified: '2026-01-01T00:00:00Z'\n---\n\nbody\n`;
const fmB = `---\ntitle: N\ncreated: '2026-01-01T00:00:00Z'\nmodified: '2026-07-26T12:00:00Z'\n---\n\nbody\n`;
const fmC = `---\ntitle: N\ncreated: '2026-01-01T00:00:00Z'\nmodified: '2026-07-26T12:00:00Z'\n---\n\nbody changed\n`;
const fmD = `---\ntitle: RENAMED\ncreated: '2026-01-01T00:00:00Z'\nmodified: '2026-01-01T00:00:00Z'\n---\n\nbody\n`;

check('stamp-only change is not a real change', sameExceptManagedStamps(fmA, fmB), true);
check('a body change is real',                  sameExceptManagedStamps(fmA, fmC), false);
check('a title change is real',                 sameExceptManagedStamps(fmA, fmD), false);
check('no frontmatter still compares',          sameExceptManagedStamps('x', 'x'), true);
check('align copies the stamps over',           alignManagedStamps(fmB, fmA), fmA);
check('align leaves other keys alone',          alignManagedStamps(fmD, fmA).includes('RENAMED'), true);
check('align is a no-op without frontmatter',   alignManagedStamps('plain', fmA), 'plain');

// The whole point: an editor lagging by one stamp must not read as an edit.
// Base carries the new stamp; "mine" is the editor's copy, still on the old
// stamp, with an edit on one line; "theirs" edits a different line.
const twoLineBase = `---\ntitle: N\nmodified: '2026-07-26T12:00:00Z'\n---\n\nalpha\nbeta\n`;
const staleMine   = `---\ntitle: N\nmodified: '2026-01-01T00:00:00Z'\n---\n\nalpha EDITED\nbeta\n`;
const stampTheirs = `---\ntitle: N\nmodified: '2026-07-26T12:00:00Z'\n---\n\nalpha\nbeta EDITED\n`;

// Three different stamps — the editor lagging while the server advanced twice
// — is the case that genuinely collides over a value nobody typed.
const thirdStamp = `---\ntitle: N\nmodified: '2026-07-26T18:00:00Z'\n---\n\nalpha\nbeta EDITED\n`;
check('three differing stamps collide without alignment',
	mergeThreeWay(twoLineBase, staleMine, thirdStamp).ok, false);
check('alignment rescues that case',
	mergeThreeWay(twoLineBase, alignManagedStamps(staleMine, twoLineBase), thirdStamp).ok, true);
check('aligning the stamp removes the false conflict',
	mergeThreeWay(twoLineBase, alignManagedStamps(staleMine, twoLineBase), stampTheirs),
	{ ok: true, text: `---\ntitle: N\nmodified: '2026-07-26T12:00:00Z'\n---\n\nalpha EDITED\nbeta EDITED\n` });

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
