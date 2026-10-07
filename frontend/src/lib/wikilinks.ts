/** Wikilink resolution: which note a `[[target]]` opens.
 *
 * The server resolves links by the same rules (backend/app/index/links.py)
 * for backlinks, the graph and OMNIA's link tools, and both halves share one
 * table of test vectors (wikilinks.test.ts / backend/tests/test_links.py) —
 * change them together.
 *
 * A target names a note by exact path (with or without `.md`), else by name.
 * Names alone are ambiguous across folders, so the shallowest match wins and
 * ties go to code-point order of the path: deterministic, and the same order
 * Python's sorted() gives. Case and Unicode normalization (NFC) are folded,
 * but a match spelled exactly as typed beats one that only folds to it: Linux
 * can hold `a/Ideas.md` beside `b/ideas.md`, and [[ideas]] means the second. */

export interface LinkableNote {
	path: string;
	/** The file name without `.md`, as the notes list reports it. */
	name: string;
}

const fold = (text: string) => text.normalize('NFC').toLowerCase();

/** Code-point order — not UTF-16 units, not the locale — to match Python. */
function byCodePoint(a: string, b: string): number {
	const ia = a[Symbol.iterator]();
	const ib = b[Symbol.iterator]();
	for (;;) {
		const x = ia.next();
		const y = ib.next();
		if (x.done || y.done) return x.done ? (y.done ? 0 : -1) : 1;
		const diff = x.value.codePointAt(0)! - y.value.codePointAt(0)!;
		if (diff !== 0) return diff;
	}
}

function shallowest<T extends LinkableNote>(matches: T[]): T | undefined {
	let best: T | undefined;
	for (const note of matches) {
		if (!best) {
			best = note;
			continue;
		}
		const depth = note.path.split('/').length - best.path.split('/').length;
		if (depth < 0 || (depth === 0 && byCodePoint(note.path, best.path) < 0)) best = note;
	}
	return best;
}

/** The shallowest of `matches`, among those spelled exactly `spelled` (NFC)
 * when there are any. */
function preferSpelled<T extends LinkableNote>(
	matches: T[],
	spelled: string,
	key: (note: T) => string
): T | undefined {
	const exact = matches.filter((n) => key(n).normalize('NFC') === spelled);
	return shallowest(exact) ?? shallowest(matches);
}

export function resolveWikilink<T extends LinkableNote>(
	target: string,
	notes: readonly T[]
): T | undefined {
	const typed = target.trim().normalize('NFC');
	const needle = typed.toLowerCase();
	if (!needle) return undefined;
	const hasExt = needle.endsWith('.md');
	const withExt = hasExt ? needle : `${needle}.md`;
	// Names are listed without the extension, so `[[Note.md]]` must still match.
	const bare = hasExt ? needle.slice(0, -3) : needle;
	const typedHasExt = /\.md$/i.test(typed);
	const paths = notes.filter((n) => fold(n.path) === withExt);
	if (paths.length) {
		return preferSpelled(paths, typedHasExt ? typed : `${typed}.md`, (n) => n.path);
	}
	const names = notes.filter((n) => fold(n.name) === bare);
	return preferSpelled(names, typedHasExt ? typed.slice(0, -3) : typed, (n) => n.name);
}
