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
import { extractNoteTags } from '../markdown';
import { readDraft, writeDraft } from '../drafts';
import { CLIENT_ID } from '../live';
import * as db from './db';
import {
	computeRevision,
	isOffline,
	pushPath,
	refreshPendingBadge,
	schedulePush
} from './engine';

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

/** A 4xx/5xx: the server answered. Anything else is a connection failure. */
function isHttpError(err: unknown): boolean {
	return err instanceof ApiError && err.status > 0;
}

/** Try the network; on connection failure serve the fallback. HTTP-level
 * errors (4xx/5xx) are real answers and pass through. */
async function netFirst<T>(call: () => Promise<T>, fallback: () => Promise<T>): Promise<T> {
	if (offline()) return fallback();
	try {
		return await call();
	} catch (err) {
		if (isHttpError(err)) throw err;
		return fallback();
	}
}

function isPending(row: db.NoteRow): boolean {
	return row.sync_status === 'dirty' || row.sync_status === 'new';
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
		// The editor saves on top of this, so it must be a revision the server
		// issued. The local sha of unsynced text never matches: the save comes
		// back stale and the merge, with this text as the ancestor, reads the
		// server copy as deleting it.
		revision: row.base_revision ?? '',
		status: 'saved'
	};
}

/** Frontmatter tags plus inline #tags, by the grammar the server indexes
 * with — so offline counts and tag pages agree with the online ones (code
 * fences skipped, an ASCII letter first). */
function tagsOf(row: db.NoteRow): Set<string> {
	let meta: Record<string, unknown> = {};
	try {
		const parsed: unknown = JSON.parse(row.meta_json);
		if (parsed && typeof parsed === 'object') meta = parsed as Record<string, unknown>;
	} catch {
		// fall through to inline scan
	}
	return new Set(extractNoteTags(row.content, meta));
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

// --- templates ---------------------------------------------------------------

const FENCE_RE = /^-{3,}[ \t]*$/;

/** A template is a body: drop its leading `---` frontmatter block, the way
 * vault/fm.parse splits it server-side (one separator newline after the
 * closing fence goes with it). The server's orphan-block heuristic in
 * templates.strip_managed_frontmatter is not reproduced here. */
function stripFrontmatter(content: string): string {
	const lines = content.split('\n');
	const isFence = (line: string) => FENCE_RE.test(line.replace(/\r$/, ''));
	if (!isFence(lines[0])) return content;
	const closing = lines.findIndex((line, i) => i > 0 && isFence(line));
	if (closing === -1) return content;
	const body = lines.slice(closing + 1).join('\n');
	return body.startsWith('\r\n') ? body.slice(2) : body.startsWith('\n') ? body.slice(1) : body;
}

/** Offline counterpart of vault/templates.render: instantiate
 * templates/<name>.md from the cache (templates are ordinary notes, so the
 * cache already holds them). Null when the template is not cached, which the
 * callers treat like the server treats a missing template. */
async function renderCachedTemplate(
	name: string,
	title: string,
	date: string
): Promise<string | null> {
	// Template names are plain file stems, never paths (templates._resolve).
	if (!name || name.includes('/') || name === '.' || name === '..') return null;
	const row = await db.getNote(`templates/${name}.md`);
	if (!row || row.sync_status === 'deleted') return null;
	const now = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
	return stripFrontmatter(row.content)
		.replaceAll('{{title}}', title)
		.replaceAll('{{date}}', date)
		.replaceAll('{{now}}', now);
}

// --- write helpers -----------------------------------------------------------

async function localSave(path: string, content: string): Promise<Note> {
	const revision = await computeRevision(content);
	const existing = await db.getNote(path);
	if (existing) await db.writeDirty(path, content, revision);
	else await db.insertNew(path, content, revision);
	await refreshPendingBadge();
	// A request failed while the engine is live: no reconnect is coming to
	// push this row, so the engine pushes it once the burst settles.
	if (!isOffline()) schedulePush();
	// Still the server's revision, as in rowToNote: the editor's next save
	// must build on what the server holds.
	return { path, content, meta: {}, revision: existing?.base_revision ?? '', status: 'saved' };
}

/** Have the engine push a row's pending text now, merged against its base as
 * a replay would. An HTTP error leaves the row pending and the caller goes on;
 * a connection failure propagates, so netFirst serves the cache. */
async function pushFirst(path: string): Promise<void> {
	try {
		await pushPath(path);
	} catch (err) {
		if (!isHttpError(err)) throw err;
	}
}

/** Open a note whose cached row holds text the server has not seen. Push it
 * first and serve the result. Handing the editor the local text as the note
 * would make it the merge ancestor, and the server copy would then read as
 * deleting it. */
async function readPending(path: string): Promise<Note> {
	await pushFirst(path);
	const row = await db.getNote(path);
	if (row?.sync_status === 'synced') return rowToNote(row);
	// Still pending: hot on another device for three rounds, or refused.
	// Open the server copy and offer the local text through the editor's
	// draft recovery. A draft already there is the editor's own, and newer.
	const note = await netApi.readNote(path);
	if (row && isPending(row) && row.content !== note.content && !(await readDraft(path))) {
		// The desktop is one window, so the tab that wrote it is this one.
		await writeDraft({
			path,
			owner: CLIENT_ID,
			content: row.content,
			// With its text, so recovery merges onto the server copy (or
			// asks) rather than saving the local text over it.
			baseRevision: row.base_revision ?? '',
			baseContent: row.base_content,
			updatedAt: Date.now()
		});
	}
	return note;
}

// --- installation ------------------------------------------------------------

let installed = false;

/** Switch the cache to `origin`'s file at sign-in (sync/db.ts keeps one per
 * server). The switch closes the open file before it opens the next, so a
 * failure left the wrappers installed at startup over no cache at all, and
 * every note open and save threw 'sync db not opened'. The network methods
 * go back instead: the app runs network-only, as when startup cannot open a
 * cache, until a sign-in opens one. Rethrows for the caller to report. */
export async function openCacheFor(origin: string): Promise<void> {
	try {
		await db.openSyncDb(origin);
	} catch (err) {
		if (installed) {
			Object.assign(api, netApi);
			installed = false;
		}
		throw err;
	}
	if (!installed) installOfflineApi();
}

/** Replace the network-only api methods with cache-aware versions. */
export function installOfflineApi(): void {
	installed = true;
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
				const local = await db.getNote(path);
				if (local && isPending(local)) return readPending(path);
				const started = Date.now();
				const note = await net.readNote(path);
				const row = await db.getNote(path);
				// Never clobber unsynced local work from a passing read; the
				// engine's merge path owns that.
				if (!row || row.sync_status === 'synced') {
					await db.adoptServer(path, note.content, note.revision, note.meta, undefined, started);
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
	api.saveNote = (path, content, baseRevision, onConflict = 'copy', options) =>
		netFirst(
			async () => {
				// A fallback save handed the editor its local text with the
				// old revision. Pushed first, that text is on the server when
				// this save comes back stale, and the editor's merge keeps it
				// instead of reading the server copy as deleting it.
				const local = await db.getNote(path);
				if (local && isPending(local)) await pushFirst(path);
				// Text the engine wrote into the row while this PUT was out
				// (a live event's merge, say) is not in the answer; only a row
				// written before it, like this editor's own fallback save, may
				// be marked synced by it.
				const started = Date.now();
				const result = await net.saveNote(path, content, baseRevision, onConflict, options);
				if (result.status === 'saved') {
					await db.adoptServer(path, result.content, result.revision, result.meta, undefined, started);
				}
				return result;
			},
			() => localSave(path, content)
		);

	api.createNote = (path, content, template) =>
		netFirst(
			async () => {
				const note = await net.createNote(path, content, template);
				await db.adoptServer(path, note.content, note.revision, note.meta);
				return note;
			},
			async () => {
				// Render the template here, as notes.py does from the path's
				// stem: the queued create replays this content, and the server
				// only applies a template when it receives no content. {{date}}
				// is the device's local date; the server's is unknowable offline.
				const stem = (path.split('/').pop() ?? path).replace(/\.md$/, '');
				const body =
					content ?? (template ? await renderCachedTemplate(template, stem, today()) : null) ?? '';
				const note = await localSave(path, body);
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
				// Same shape as meta.py open_daily: the `daily` template with
				// title = date = day. `day` comes from the device's clock, not
				// the server's configured timezone, which is unknowable offline.
				const content = (await renderCachedTemplate('daily', day, day)) ?? `# ${day}\n\n`;
				await localSave(path, content);
				await db.enqueueOp('create', path);
				await refreshPendingBadge();
				return { path, created: true };
			}
		);
}
