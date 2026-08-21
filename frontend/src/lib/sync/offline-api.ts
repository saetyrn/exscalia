// Desktop only: wraps the `api` object so reads fall back to the local cache
// and writes land locally and queue when the server is unreachable. Installed
// once at startup; the rest of the app (including the note editor's
// save/merge loop) never knows whether a response came from the network or
// the cache.

import {
	api,
	ApiError,
	type Note,
	type NoteListItem,
	type SearchResult,
	type TagCount
} from '../api';
import * as db from './db';
import { computeRevision, isOffline, refreshPendingBadge } from './engine';

type Api = typeof api;

/** The untouched network methods, captured before installOfflineApi mutates
 * the shared object. The sync engine must call these — never the wrapped
 * versions — so a network failure mid-replay surfaces as an error instead of
 * silently "succeeding" against the cache and corrupting merge bases. */
export const netApi: Api = { ...api };

function offline(): boolean {
	return isOffline();
}

function offlineError(): never {
	throw new ApiError(0, 'Offline');
}

/** Try the network; on connection failure serve the fallback. HTTP-level
 * errors (4xx/5xx) are real answers and pass through. */
async function netFirst<T>(call: () => Promise<T>, fallback: () => Promise<T>): Promise<T> {
	if (offline()) return fallback();
	try {
		return await call();
	} catch (err) {
		if (err instanceof ApiError && err.status > 0) throw err;
		return fallback();
	}
}

// --- offline derivations -----------------------------------------------------

function rowToListItem(row: db.NoteRow): NoteListItem {
	// Board-ness comes from the cached frontmatter, so board notes stay hidden
	// from the sidebar tree offline too — otherwise they reappear the moment
	// the desktop app loses its connection.
	let board = false;
	try {
		board = JSON.parse(row.meta_json || '{}').task_board === true;
	} catch {
		// Unparsable cached meta — treat it as an ordinary note.
	}
	return { path: row.path, name: row.name, pinned: Boolean(row.pinned), board };
}

function rowToNote(row: db.NoteRow): Note {
	let meta: Record<string, unknown> = {};
	try {
		meta = JSON.parse(row.meta_json);
	} catch {
		// Corrupt cache row; content is still usable.
	}
	return {
		path: row.path,
		content: row.content,
		meta,
		revision: row.revision ?? '',
		status: 'saved'
	};
}

const TAG_RE = /(^|\s)#([\p{L}\p{N}_/-]+)/gu;

function tagsOf(row: db.NoteRow): Set<string> {
	const tags = new Set<string>();
	try {
		const meta = JSON.parse(row.meta_json) as { tags?: unknown };
		if (Array.isArray(meta.tags)) {
			for (const t of meta.tags) if (typeof t === 'string') tags.add(t.replace(/^#/, ''));
		}
	} catch {
		// fall through to inline scan
	}
	for (const match of row.content.matchAll(TAG_RE)) tags.add(match[2]);
	return tags;
}

async function offlineSearch(q: string): Promise<SearchResult[]> {
	const needle = q.trim().toLowerCase();
	if (!needle) return [];
	const results: (SearchResult & { _score: number })[] = [];
	for (const row of await db.allNotes()) {
		const title = row.name.toLowerCase();
		const content = row.content.toLowerCase();
		let score = 0;
		if (title === needle) score = 100;
		else if (title.startsWith(needle)) score = 60;
		else if (title.includes(needle)) score = 40;
		else {
			const at = content.indexOf(needle);
			if (at === -1) continue;
			score = 10;
		}
		const at = content.indexOf(needle);
		const snippet =
			at === -1
				? row.content.slice(0, 160)
				: row.content.slice(Math.max(0, at - 60), at + 100);
		results.push({
			type: 'note',
			path: row.path,
			title: row.name,
			snippet: snippet.replace(/\s+/g, ' ').trim(),
			score,
			_score: score
		});
	}
	return results.sort((a, b) => b._score - a._score).slice(0, 50);
}

function today(): string {
	const now = new Date();
	const pad = (n: number) => String(n).padStart(2, '0');
	return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

// --- write helpers -----------------------------------------------------------

async function localSave(path: string, content: string): Promise<Note> {
	const revision = await computeRevision(content);
	const existing = await db.getNote(path);
	if (existing) await db.writeDirty(path, content, revision);
	else await db.insertNew(path, content, revision);
	await refreshPendingBadge();
	return { path, content, meta: {}, revision, status: 'saved' };
}

// --- installation ------------------------------------------------------------

/** Replace the network-only api methods with cache-aware versions. */
export function installOfflineApi(): void {
	const net = netApi;

	// Reads ------------------------------------------------------------------
	api.listNotes = () =>
		netFirst(
			async () => {
				const notes = await net.listNotes();
				return notes;
			},
			async () => (await db.allNotes()).map(rowToListItem)
		);

	api.listFolders = () =>
		netFirst(
			() => net.listFolders(),
			() => db.allFolders()
		);

	api.readNote = (path) =>
		netFirst(
			async () => {
				const note = await net.readNote(path);
				const row = await db.getNote(path);
				// Never clobber unsynced local work from a passing read; the
				// engine's merge path owns that.
				if (!row || row.sync_status === 'synced') {
					await db.adoptServer(path, note.content, note.revision, note.meta);
				}
				return note;
			},
			async () => {
				const row = await db.getNote(path);
				if (!row || row.sync_status === 'deleted') throw new ApiError(404, 'Not cached');
				return rowToNote(row);
			}
		);

	api.listTags = () =>
		netFirst(
			() => net.listTags(),
			async () => {
				const counts = new Map<string, number>();
				for (const row of await db.allNotes()) {
					for (const tag of tagsOf(row)) counts.set(tag, (counts.get(tag) ?? 0) + 1);
				}
				return [...counts.entries()]
					.map(([tag, count]): TagCount => ({ tag, count }))
					.sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
			}
		);

	api.notesForTag = (tag) =>
		netFirst(
			() => net.notesForTag(tag),
			async () => {
				const rows = (await db.allNotes()).filter((row) => tagsOf(row).has(tag));
				return rows.map((row) => ({ path: row.path, title: row.name, modified: '' }));
			}
		);

	api.search = (q) =>
		netFirst(
			() => net.search(q),
			() => offlineSearch(q)
		);

	api.unifiedSearch = (q, filters) =>
		netFirst(
			() => net.unifiedSearch(q, filters),
			async () => ({
				query: q,
				semantic_available: false,
				results: await offlineSearch(q)
			})
		);

	// Writes -----------------------------------------------------------------
	api.saveNote = (path, content, baseRevision, onConflict = 'copy') =>
		netFirst(
			async () => {
				const result = await net.saveNote(path, content, baseRevision, onConflict);
				if (result.status === 'saved') {
					await db.adoptServer(path, result.content, result.revision, result.meta);
				}
				return result;
			},
			() => localSave(path, content)
		);

	api.createNote = (path, content) =>
		netFirst(
			async () => {
				const note = await net.createNote(path, content);
				await db.adoptServer(path, note.content, note.revision, note.meta);
				return note;
			},
			async () => {
				const note = await localSave(path, content ?? '');
				await db.enqueueOp('create', path);
				await refreshPendingBadge();
				return note;
			}
		);

	api.deleteNote = (path) =>
		netFirst(
			async () => {
				const result = await net.deleteNote(path);
				await db.removeNote(path);
				return result;
			},
			async () => {
				const row = await db.getNote(path);
				if (row?.sync_status === 'new') {
					// Never existed server-side; no op needed.
					await db.removeNote(path);
				} else {
					await db.markDeleted(path);
					await db.enqueueOp('delete', path);
				}
				await refreshPendingBadge();
				return { ok: true };
			}
		);

	api.renameNote = (path, newPath, newTitle) =>
		netFirst(
			async () => {
				const result = await net.renameNote(path, newPath, newTitle);
				await db.moveNote(path, newPath);
				return result;
			},
			async () => {
				const row = await db.getNote(path);
				if (!row) throw new ApiError(404, 'Not cached');
				await db.moveNote(path, newPath);
				await db.rewriteOpPaths(path, newPath);
				if (row.sync_status !== 'new') {
					await db.enqueueOp('rename', path, newPath, { new_title: newTitle ?? null });
				}
				await refreshPendingBadge();
				return { path: newPath };
			}
		);

	api.pinNote = (path, pinned) =>
		netFirst(
			async () => {
				const result = await net.pinNote(path, pinned);
				await db.setPinned(path, pinned);
				return result;
			},
			async () => {
				await db.setPinned(path, pinned);
				await db.enqueueOp('pin', path, null, { pinned });
				await refreshPendingBadge();
				return { path, pinned };
			}
		);

	api.createFolder = (path) =>
		netFirst(
			async () => {
				const result = await net.createFolder(path);
				await db.addFolder(path);
				return result;
			},
			async () => {
				await db.addFolder(path);
				await db.enqueueOp('create_folder', path);
				await refreshPendingBadge();
				return { path };
			}
		);

	api.renameFolder = (path, newPath) =>
		netFirst(
			async () => {
				const result = await net.renameFolder(path, newPath);
				await db.renameFolderTree(path, newPath);
				return result;
			},
			async () => {
				await db.renameFolderTree(path, newPath);
				await db.enqueueOp('rename_folder', path, newPath);
				await refreshPendingBadge();
				return { path: newPath, moved: 0 };
			}
		);

	api.deleteFolder = (path) =>
		netFirst(
			async () => {
				const result = await net.deleteFolder(path);
				await db.removeFolderTree(path);
				return result;
			},
			async () => {
				// Deleting a whole folder is destructive enough that doing it
				// blind against a stale cache is not worth it.
				offlineError();
			}
		);

	api.openDaily = (date) =>
		netFirst(
			() => net.openDaily(date),
			async () => {
				const day = date ?? today();
				const path = `daily/${day}.md`;
				const existing = await db.getNote(path);
				if (existing && existing.sync_status !== 'deleted') return { path, created: false };
				await localSave(path, `# ${day}\n\n`);
				await db.enqueueOp('create', path);
				await refreshPendingBadge();
				return { path, created: true };
			}
		);
}
