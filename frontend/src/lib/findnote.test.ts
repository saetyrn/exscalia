// How a wikilink target resolves to a note. Lookups go through an index built
// once per note list (a chat reply resolves every link on every render), so
// these pin the resolution order the old list scan had, and that a new list
// — refreshNotes replaces the array — is what the index answers from.
import { describe, expect, it } from 'vitest';

import type { NoteListItem } from './api';
import { findNoteByName, vault } from './stores.svelte';

const note = (path: string, name = path.split('/').pop()!.replace(/\.md$/, '')): NoteListItem => ({
	path,
	name,
	pinned: false
});

/** The path `name` resolves to against `notes`, or undefined. */
function resolve(notes: NoteListItem[], name: string): string | undefined {
	vault.notes = notes;
	return findNoteByName(name)?.path;
}

describe('findNoteByName', () => {
	it('prefers an exact path over a note of that name', () => {
		// Names come from the list, so this one can disagree with its path.
		const notes = [note('Deep/Plan.md'), note('Plan.md', 'Roadmap')];
		expect(resolve(notes, 'Plan')).toBe('Plan.md');
		expect(resolve(notes, 'Deep/Plan')).toBe('Deep/Plan.md');
	});

	it('takes the only note of that name, however deep', () => {
		expect(resolve([note('a/b/c/Solo.md'), note('Other.md')], 'Solo')).toBe('a/b/c/Solo.md');
	});

	it('takes the shallowest of several, then the first alphabetically', () => {
		expect(resolve([note('a/b/Plan.md'), note('z/Plan.md')], 'Plan')).toBe('z/Plan.md');
		const tied = [note('work/Plan.md'), note('home/Plan.md')];
		expect(resolve(tied, 'Plan')).toBe('home/Plan.md');
		expect(resolve([...tied].reverse(), 'Plan')).toBe('home/Plan.md');
	});

	it('takes the exact spelling when two paths differ only by case', () => {
		const twins = [note('Inbox/Todo.md'), note('inbox/todo.md')];
		expect(resolve(twins, 'inbox/todo')).toBe('inbox/todo.md');
		expect(resolve(twins, 'Inbox/Todo')).toBe('Inbox/Todo.md');
		// Neither spelling typed: the shared tie-break, not list order.
		expect(resolve([...twins].reverse(), 'INBOX/TODO')).toBe('Inbox/Todo.md');
	});

	it('accepts the target with or without .md', () => {
		const notes = [note('Work/Plan.md'), note('x/y/Ideas.md')];
		expect(resolve(notes, 'Work/Plan.md')).toBe('Work/Plan.md');
		expect(resolve(notes, 'Ideas.md')).toBe('x/y/Ideas.md');
	});

	it('ignores case and surrounding space', () => {
		const notes = [note('Work/Plan.md'), note('x/Ideas.md')];
		expect(resolve(notes, 'WORK/PLAN')).toBe('Work/Plan.md');
		expect(resolve(notes, '  ideas ')).toBe('x/Ideas.md');
	});

	it('finds nothing for a missing or empty name', () => {
		const notes = [note('Plan.md')];
		expect(resolve(notes, 'Missing')).toBeUndefined();
		expect(resolve(notes, '   ')).toBeUndefined();
	});

	it('answers from the note list as it is now', () => {
		vault.notes = [note('Plan.md')];
		expect(findNoteByName('Later')).toBeUndefined();
		// A note created after the first lookup.
		vault.notes = [...vault.notes, note('notes/Later.md')];
		expect(findNoteByName('Later')?.path).toBe('notes/Later.md');
		// And one deleted since.
		vault.notes = [note('Plan.md')];
		expect(findNoteByName('Later')).toBeUndefined();
		expect(findNoteByName('Plan')?.path).toBe('Plan.md');
	});
});
