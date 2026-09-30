import { describe, expect, it } from 'vitest';
import type { NoteListItem } from './api';
import { findNoteByName, vault } from './stores.svelte';
import { resolveWikilink } from './wikilinks';

// --- the shared vectors ------------------------------------------------------
// Twin of the table in backend/tests/test_links.py — keep them equal.

const NOTES = [
	'Welcome.md',
	'Index.md',
	'work/Spec.md',
	'a/Plan.md',
	'b/Plan.md',
	'Journal.md',
	'archive/Journal.md',
	'z/Todo.md',
	'a/b/Todo.md',
	'B/Note.md',
	'a/Note.md',
	'notes/Cafe\u0301.md',
	'R\u00e9sum\u00e9.md',
	'\ufffd/Dup.md',
	'\u{20000}/Dup.md',
	'a/Ideas.md',
	'b/ideas.md',
	'Case/Memo.md',
	'case/memo.md'
];

const VECTORS: [string, string | null][] = [
	['work/Spec.md', 'work/Spec.md'], // exact path
	['work/Spec', 'work/Spec.md'], // exact path without .md
	['Spec', 'work/Spec.md'], // name
	['Spec.md', 'work/Spec.md'], // name with .md
	['WORK/spec', 'work/Spec.md'], // case folded
	['  Spec  ', 'work/Spec.md'], // padding trimmed
	['Index.md', 'Index.md'],
	['Plan', 'a/Plan.md'], // same depth: code-point order
	['Plan.md', 'a/Plan.md'],
	['b/Plan', 'b/Plan.md'], // a path beats the name tie-break
	['B/plan.md', 'b/Plan.md'],
	['Journal', 'Journal.md'], // root beats a folder
	['archive/Journal', 'archive/Journal.md'],
	['Todo', 'z/Todo.md'], // shallowest beats alphabetical
	['Note', 'B/Note.md'], // code points: 'B' < 'a' (localeCompare disagrees)
	['caf\u00e9', 'notes/Cafe\u0301.md'], // NFC target, NFD file
	['Re\u0301sume\u0301', 'R\u00e9sum\u00e9.md'], // NFD target, NFC file
	['Dup', '\ufffd/Dup.md'], // code points, not UTF-16 units
	['ideas', 'b/ideas.md'], // the exact spelling beats code-point order
	['Ideas', 'a/Ideas.md'],
	['IDEAS', 'a/Ideas.md'], // no exact spelling: code points
	['ideas.md', 'b/ideas.md'],
	['case/memo', 'case/memo.md'], // a path spelled exactly beats code-point order
	['Case/Memo.md', 'Case/Memo.md'],
	['CASE/MEMO', 'Case/Memo.md'], // no exact spelling: code points, 'C' < 'c'
	['memo', 'case/memo.md'],
	['Memo', 'Case/Memo.md'],
	['Nope', null],
	['work/Nope', null],
	['Spec/extra', null],
	['', null],
	['   ', null]
];

/** A notes-list entry: `name` is the file name without `.md`, as the server lists it. */
const entry = (path: string) => ({ path, name: path.split('/').pop()!.replace(/\.md$/, '') });

describe('resolveWikilink', () => {
	const notes = NOTES.map(entry);

	it.each(VECTORS)('resolves %j to %j', (target, expected) => {
		expect(resolveWikilink(target, notes)?.path ?? null).toBe(expected);
		// The note list's order never matters.
		expect(resolveWikilink(target, [...notes].reverse())?.path ?? null).toBe(expected);
	});

	it('returns the list entry itself', () => {
		const spec = notes.find((n) => n.path === 'work/Spec.md');
		expect(resolveWikilink('Spec', notes)).toBe(spec);
	});
});

// What a click opens (the editor resolves through its note-list index) must be
// what the server counts, so the editor's lookup answers the same table.
describe('findNoteByName', () => {
	const notes: NoteListItem[] = NOTES.map((path) => ({ ...entry(path), pinned: false }));

	it.each(VECTORS)('opens %j as %j', (target, expected) => {
		vault.notes = notes;
		expect(findNoteByName(target)?.path ?? null).toBe(expected);
		vault.notes = [...notes].reverse();
		expect(findNoteByName(target)?.path ?? null).toBe(expected);
	});
});
