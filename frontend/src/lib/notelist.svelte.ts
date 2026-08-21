/** Shared note-browser view state: how notes are sorted, which folders are
 * collapsed.
 *
 * Both the desktop sidebar and the mobile /notes browser draw the same tree,
 * and they already persisted to the same two localStorage keys — but each held
 * its own copy in memory, so changing the sort in one left the other showing
 * the old order until a reload. One rune store, two readers.
 */
import type { NoteSort } from './notetree';

const SORT_KEY = 'omnia.noteSort';
const COLLAPSED_KEY = 'omnia.collapsed';

const VALID: NoteSort[] = ['name', 'created', 'modified', 'manual'];

function readSort(): NoteSort {
	try {
		const raw = localStorage.getItem(SORT_KEY) as NoteSort | null;
		return raw && VALID.includes(raw) ? raw : 'name';
	} catch {
		return 'name';
	}
}

function readCollapsed(): Record<string, boolean> {
	try {
		const raw = JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? '{}');
		return raw && typeof raw === 'object' ? raw : {};
	} catch {
		return {};
	}
}

export const noteView = $state({
	sort: readSort(),
	/** Keyed by folder path; the sidebar also uses the pseudo-paths '::pinned'
	 * and '::tags' for its two synthetic sections. Absent = expanded. */
	collapsed: readCollapsed()
});

function persistCollapsed() {
	try {
		localStorage.setItem(COLLAPSED_KEY, JSON.stringify(noteView.collapsed));
	} catch {
		/* private mode — the arrangement just won't survive a reload */
	}
}

export function setNoteSort(mode: NoteSort) {
	noteView.sort = mode;
	try {
		localStorage.setItem(SORT_KEY, mode);
	} catch {
		/* private mode — the choice just won't persist */
	}
}

export function toggleFolder(path: string) {
	noteView.collapsed[path] = !noteView.collapsed[path];
	persistCollapsed();
}

/** Expand a folder and all its ancestors, so a freshly created item is
 * visible wherever it landed. */
export function expandTo(folderPath: string) {
	let acc = '';
	for (const part of folderPath.split('/')) {
		acc = acc ? `${acc}/${part}` : part;
		noteView.collapsed[acc] = false;
	}
	persistCollapsed();
}
