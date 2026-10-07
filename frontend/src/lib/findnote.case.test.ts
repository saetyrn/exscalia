// Which note a wikilink opens. Case and Unicode form are folded so a typed
// [[Café]] reaches a note whose name was pasted in NFD, but when Linux holds
// both `Ideas.md` and `ideas.md` the exact spelling must win over list order.
import { beforeEach, describe, expect, it } from 'vitest';

import type { NoteListItem } from './api';
import { findNoteByName, vault } from './stores.svelte';

const note = (path: string) =>
	({ path, name: path.split('/').pop()!.replace(/\.md$/, '') }) as NoteListItem;

const NFC = 'Café'.normalize('NFC');
const NFD = 'Café'.normalize('NFD');

describe('findNoteByName', () => {
	beforeEach(() => {
		vault.notes = [];
	});

	it('prefers the exact-case path over an earlier case variant', () => {
		vault.notes = [note('Ideas.md'), note('ideas.md')];
		expect(findNoteByName('ideas')?.path).toBe('ideas.md');
		expect(findNoteByName('Ideas')?.path).toBe('Ideas.md');
		expect(findNoteByName('IDEAS')?.path).toBe('Ideas.md');
	});

	it('prefers the exact-case name among bare-name matches', () => {
		vault.notes = [note('A/Ideas.md'), note('B/ideas.md')];
		expect(findNoteByName('ideas')?.path).toBe('B/ideas.md');
		expect(findNoteByName('Ideas')?.path).toBe('A/Ideas.md');
		// No exact case: the shallowest, then alphabetical, as before.
		expect(findNoteByName('IDEAS')?.path).toBe('A/Ideas.md');
	});

	it('matches across NFC and NFD', () => {
		vault.notes = [note(`${NFD}.md`)];
		expect(findNoteByName(NFC)?.path).toBe(`${NFD}.md`);
		vault.notes = [note(`Travel/${NFC}.md`)];
		expect(findNoteByName(NFD)?.path).toBe(`Travel/${NFC}.md`);
	});

	it('still folds case when nothing matches exactly', () => {
		vault.notes = [note('Plan.md')];
		expect(findNoteByName('plan.MD')?.path).toBe('Plan.md');
		expect(findNoteByName('  plan ')?.path).toBe('Plan.md');
		expect(findNoteByName('missing')).toBeUndefined();
		expect(findNoteByName('  ')).toBeUndefined();
	});
});
