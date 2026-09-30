/** The vault folder tree, as a pure function of the note list.
 *
 * This used to live inside Sidebar.svelte. The mobile /notes browser needs the
 * exact same tree — same hidden roots, same sort rules, same saved
 * arrangement — and a second copy would have drifted the first time either
 * side gained a folder to hide. Being plain TypeScript, it is also directly
 * testable (notetree.test.ts) in a way the component's closure never was.
 */

export interface TreeNode {
	name: string;
	path: string;
	/** null = file. */
	children: TreeNode[] | null;
}

/** How notes are ordered inside their folder. 'name' is pure alphabetical;
 * 'created'/'modified' sort by the note's own timestamps, newest first;
 * 'manual' is the hand-dragged arrangement (the only mode where dragging
 * notes in the sidebar does anything). */
export type NoteSort = 'name' | 'created' | 'modified' | 'manual';

export const NOTE_SORTS: { value: NoteSort; label: string }[] = [
	{ value: 'name', label: 'Alphabetical' },
	{ value: 'created', label: 'Date created' },
	{ value: 'modified', label: 'Date modified' },
	{ value: 'manual', label: 'Manual order' }
];

/** App-managed folders/notes with dedicated navigation of their own, so they
 * never appear as ordinary entries in the vault tree. */
export const UNIVERSAL_FOLDERS = new Set(['daily', 'omnia', 'templates', 'media', 'paper']);
export const UNIVERSAL_PATHS = new Set(['Tasks Inbox.md']);

/** The subset of a NoteListItem the tree cares about. */
export interface TreeNote {
	path: string;
	/** A folder's task board: it belongs to the folder row, not the tree. */
	board?: boolean;
	created?: string | null;
	modified?: string | null;
}

/** Does this note belong in the browsable tree at all? */
export function isTreeNote(note: TreeNote): boolean {
	return (
		!UNIVERSAL_FOLDERS.has(note.path.split('/')[0]) &&
		!UNIVERSAL_PATHS.has(note.path) &&
		!note.board
	);
}

/** Does this folder belong in the browsable tree at all? */
export function isTreeFolder(folder: string): boolean {
	return !UNIVERSAL_FOLDERS.has(folder.split('/')[0]);
}

export interface BuildTreeInput {
	/** The whole note list; universal folders and boards are filtered here. */
	notes: TreeNote[];
	/** The whole folder list, so empty folders still show. */
	folders?: string[];
	sortMode?: NoteSort;
	/** Saved folder arrangement (vault.folderOrder). */
	folderOrder?: string[];
	/** Saved note arrangement, used only by sortMode 'manual'. */
	noteOrder?: string[];
}

export function buildTree({
	notes,
	folders = [],
	sortMode = 'name',
	folderOrder = [],
	noteOrder = []
}: BuildTreeInput): TreeNode[] {
	const root: TreeNode[] = [];
	const ensureFolder = (folderPath: string): TreeNode[] => {
		let level = root;
		let acc = '';
		for (const part of folderPath.split('/')) {
			acc = acc ? `${acc}/${part}` : part;
			let node = level.find((n) => n.name === part && n.children !== null);
			if (!node) {
				node = { name: part, path: acc, children: [] };
				level.push(node);
			}
			level = node.children!;
		}
		return level;
	};

	// Folders first (so empty ones show), then files into their folders.
	for (const f of folders.filter(isTreeFolder)) ensureFolder(f);
	const visible = notes.filter(isTreeNote);
	for (const note of visible) {
		const parts = note.path.split('/');
		const dir = parts.slice(0, -1).join('/');
		const level = dir ? ensureFolder(dir) : root;
		level.push({ name: parts[parts.length - 1], path: note.path, children: null });
	}

	const dates = new Map(
		visible.map((n) => [n.path, { created: n.created ?? '', modified: n.modified ?? '' }])
	);
	// Folders and notes each follow their saved arrangement; anything not in it
	// (made since the last rearrange) falls to the end of its group,
	// alphabetical.
	const fRank = new Map(folderOrder.map((path, index) => [path, index]));
	const folderRank = (node: TreeNode) => fRank.get(node.path) ?? Number.MAX_SAFE_INTEGER;
	const nRank = new Map(noteOrder.map((path, index) => [path, index]));
	const noteRank = (node: TreeNode) => nRank.get(node.path) ?? Number.MAX_SAFE_INTEGER;

	const sort = (nodes: TreeNode[]) => {
		nodes.sort((a, b) => {
			if ((a.children === null) !== (b.children === null)) return a.children === null ? 1 : -1;
			if (a.children !== null && b.children !== null) {
				const byOrder = folderRank(a) - folderRank(b);
				if (byOrder !== 0) return byOrder;
			}
			if (a.children === null && b.children === null) {
				if (sortMode === 'created' || sortMode === 'modified') {
					const da = dates.get(a.path)?.[sortMode] ?? '';
					const db = dates.get(b.path)?.[sortMode] ?? '';
					if (da !== db) {
						// Newest first; notes with no date sink to the bottom.
						if (!da) return 1;
						if (!db) return -1;
						return da < db ? 1 : -1;
					}
				} else if (sortMode === 'manual') {
					const byOrder = noteRank(a) - noteRank(b);
					if (byOrder !== 0) return byOrder;
				}
				// 'name' (and any tie) falls through to pure alphabetical.
			}
			return a.name.localeCompare(b.name);
		});
		for (const n of nodes) if (n.children) sort(n.children);
	};
	sort(root);
	return root;
}

/** The manual arrangement after a sidebar drop: `moving` lifted out and set
 * down right after `anchor`, or first among `dir`'s notes when `anchor` is
 * null. Built from the saved `order` (vault.noteOrder), never from the tree as
 * displayed, so a drop can not save a name or date sort as the arrangement.
 * `displayed` only drops stale entries and places notes the arrangement has
 * not seen yet, appended in display order — where manual mode shows them. */
export function moveNoteInOrder(
	order: string[],
	displayed: string[],
	moving: string,
	anchor: string | null,
	dir: string
): string[] {
	const dirOf = (path: string) => path.split('/').slice(0, -1).join('/');
	const shown = new Set(displayed);
	const placed = new Set(order);
	const next = [
		...order.filter((path) => shown.has(path)),
		...displayed.filter((path) => !placed.has(path))
	].filter((path) => path !== moving);
	const at =
		anchor === null
			? next.findIndex((path) => dirOf(path) === dir)
			: next.includes(anchor)
				? next.indexOf(anchor) + 1
				: -1;
	// An empty folder (or an anchor that has gone) has no slot to aim at.
	if (at === -1) next.push(moving);
	else next.splice(at, 0, moving);
	return next;
}

/** Flatten a tree to the paths of every node that survives `keep`, used by the
 * /notes browser to decide whether a folder has anything left under a filter.
 * Returns a new tree; folders with no surviving descendants are dropped. */
export function filterTree(nodes: TreeNode[], keep: (node: TreeNode) => boolean): TreeNode[] {
	const out: TreeNode[] = [];
	for (const node of nodes) {
		if (node.children === null) {
			if (keep(node)) out.push(node);
			continue;
		}
		const children = filterTree(node.children, keep);
		// A folder survives on its own name too, so searching for a folder
		// shows what is inside it rather than an empty row.
		if (children.length > 0) out.push({ ...node, children });
		else if (keep(node)) out.push({ ...node, children: [] });
	}
	return out;
}

/** Display name for a note node — the tree stores real filenames. */
export function noteLabel(name: string): string {
	return name.replace(/\.md$/, '');
}
