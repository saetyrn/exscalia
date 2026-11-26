// Desktop only: the local note cache and offline write queue, in SQLite via
// tauri-plugin-sql. This is the durable copy — webview storage is treated as
// disposable, unsynced offline edits are not.
//
// Content edits are row state (`sync_status = 'dirty'`, with `base_*` as the
// three-way-merge ancestor); only structural operations (create, delete,
// rename, pin, folders) go through the FIFO `ops` queue, so compositions like
// create → edit → rename replay in the right order.

import Database from '@tauri-apps/plugin-sql';
import { DEFAULT_SERVER } from '../runtime';

export type SyncStatus = 'synced' | 'dirty' | 'new' | 'deleted';

export interface NoteRow {
	path: string;
	name: string;
	pinned: number;
	content: string;
	revision: string | null;
	base_content: string;
	base_revision: string | null;
	meta_json: string;
	sync_status: SyncStatus;
	fetched_at: number | null;
	updated_at: number | null;
}

export type OpKind =
	| 'create'
	| 'delete'
	| 'rename'
	| 'pin'
	| 'create_folder'
	| 'rename_folder'
	| 'delete_folder';

export interface OpRow {
	id: number;
	op: OpKind;
	path: string;
	new_path: string | null;
	payload_json: string | null;
	created_at: number;
	attempts: number;
	last_error: string | null;
}

export interface ConflictRow {
	id: number;
	path: string;
	conflict_path: string;
	created_at: number;
	seen: number;
}

const SCHEMA = `
CREATE TABLE IF NOT EXISTS notes (
  path TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0,
  content TEXT NOT NULL,
  revision TEXT,
  base_content TEXT NOT NULL DEFAULT '',
  base_revision TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}',
  sync_status TEXT NOT NULL DEFAULT 'synced',
  fetched_at INTEGER,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS folders (path TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS ops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  op TEXT NOT NULL,
  path TEXT NOT NULL,
  new_path TEXT,
  payload_json TEXT,
  created_at INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  conflict_path TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT);
`;

/** The cache's schema version, kept in the file's PRAGMA user_version. Bump it
 * with each MIGRATIONS step. */
export const SCHEMA_VERSION = 1;

/** MIGRATIONS[v] takes a file from version v - 1 to v. SCHEMA is always the
 * current shape, so a fresh file already has what the steps add. The plugin's
 * connection pool gives no transaction across execute() calls, so a step cut
 * off halfway runs again on the next open: every step must be idempotent,
 * checking before it alters, e.g.
 *
 *   2: async (conn) => {
 *     const cols = await conn.select<{ name: string }[]>('PRAGMA table_info(notes)');
 *     if (!cols.some((c) => c.name === 'last_error'))
 *       await conn.execute('ALTER TABLE notes ADD COLUMN last_error TEXT');
 *   }
 *
 * Version 1 is the baseline: the tables as they were before versioning. */
const MIGRATIONS: Record<number, (conn: Database) => Promise<void>> = {};

async function migrate(conn: Database): Promise<void> {
	const [row] = await conn.select<{ user_version: number }[]>('PRAGMA user_version');
	const found = row?.user_version ?? 0;
	// Written by a newer build (this one was downgraded): its tables may not
	// be the ones these statements name. Leave the file as it is.
	if (found > SCHEMA_VERSION) throw new Error('sync cache schema is newer than this app');
	for (const stmt of SCHEMA.split(';')) {
		if (stmt.trim()) await conn.execute(stmt);
	}
	// 0 is a fresh file or one from before versioning; both are version 1.
	for (let v = Math.max(found, 1) + 1; v <= SCHEMA_VERSION; v++) await MIGRATIONS[v](conn);
	if (found !== SCHEMA_VERSION) await conn.execute(`PRAGMA user_version = ${SCHEMA_VERSION}`);
}

let db: Database | null = null;
/** The server the open file belongs to. */
let dbOrigin: string | null = null;
/** Opens run one at a time, so the last server asked for is the one left open. */
let opening: Promise<void> = Promise.resolve();

/** One cache file per server. The queue holds deletes, renames and edits
 * addressed to paths on the server they were made against; replayed into
 * another vault they would trash or overwrite whatever sits at those paths
 * there. Pointing the app at another server leaves this one's unsynced work
 * in its own file until the app points back. The default server keeps the
 * original name, so existing installs keep their cache. */
async function cacheFile(origin: string): Promise<string> {
	if (origin === DEFAULT_SERVER) return 'sqlite:exscalia.db';
	const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(origin));
	const hex = Array.from(new Uint8Array(digest).slice(0, 6), (b) =>
		b.toString(16).padStart(2, '0')
	).join('');
	return `sqlite:exscalia-${hex}.db`;
}

/** Open the cache for `origin`, the configured server (apiBase()). A repeat
 * call for the same server is a no-op; a call for another server closes the
 * open file and opens that server's. */
export function openSyncDb(origin: string): Promise<void> {
	const next = opening.then(() => switchTo(origin.replace(/\/+$/, '')));
	opening = next.catch(() => {});
	return next;
}

async function switchTo(origin: string): Promise<void> {
	if (db && dbOrigin === origin) return;
	if (db) {
		const old = db;
		db = null;
		dbOrigin = null;
		try {
			await old.close(old.path);
		} catch {
			// At worst a leaked handle; the new server's file must still open.
		}
	}
	const conn = await Database.load(await cacheFile(origin));
	try {
		await migrate(conn);
	} catch (err) {
		await conn.close(conn.path).catch(() => {});
		throw err;
	}
	db = conn;
	dbOrigin = origin;
}

/** False while no cache is open, as after a failed openSyncDb: the app then
 * runs network-only. */
export function isOpen(): boolean {
	return db !== null;
}

function must(): Database {
	if (!db) throw new Error('sync db not opened');
	return db;
}

// --- notes ------------------------------------------------------------------

export async function allNotes(): Promise<NoteRow[]> {
	return must().select<NoteRow[]>(
		"SELECT * FROM notes WHERE sync_status != 'deleted' ORDER BY path"
	);
}

/** The scalar columns of a row: what a manifest diff reads. */
export type NoteHead = Pick<
	NoteRow,
	'path' | 'name' | 'pinned' | 'base_revision' | 'sync_status' | 'meta_json'
>;

/** Every live row without its bodies. allNotes() carries content and
 * base_content, two copies of each note, across the IPC bridge. */
export async function noteHeads(): Promise<NoteHead[]> {
	return must().select<NoteHead[]>(
		`SELECT path, name, pinned, base_revision, sync_status, meta_json
		 FROM notes WHERE sync_status != 'deleted' ORDER BY path`
	);
}

export async function getNote(path: string): Promise<NoteRow | null> {
	const rows = await must().select<NoteRow[]>('SELECT * FROM notes WHERE path = $1', [path]);
	return rows[0] ?? null;
}

export async function countNotes(): Promise<number> {
	const rows = await must().select<{ n: number }[]>('SELECT COUNT(*) AS n FROM notes');
	return rows[0]?.n ?? 0;
}

function nameFor(path: string): string {
	return (path.split('/').pop() ?? path).replace(/\.md$/, '');
}

/** Adopt the server's version of a note wholesale (fetch, save response,
 * replay success): content and base move together, status becomes synced.
 * With `unlessUpdatedAfter` (a Date.now() taken before the request went out)
 * a row written after that moment is left alone: the answer never saw that
 * text, so marking it synced would drop it from the next push. False when the
 * row was left alone. */
export async function adoptServer(
	path: string,
	content: string,
	revision: string,
	meta: Record<string, unknown>,
	pinned?: boolean,
	unlessUpdatedAfter?: number
): Promise<boolean> {
	const now = Date.now();
	const result = await must().execute(
		`INSERT INTO notes (path, name, pinned, content, revision, base_content, base_revision,
		                    meta_json, sync_status, fetched_at, updated_at)
		 VALUES ($1, $2, COALESCE($3, 0), $4, $5, $4, $5, $6, 'synced', $7, $7)
		 ON CONFLICT(path) DO UPDATE SET
		   name = $2,
		   pinned = COALESCE($3, notes.pinned),
		   content = $4, revision = $5, base_content = $4, base_revision = $5,
		   meta_json = $6, sync_status = 'synced', fetched_at = $7, updated_at = $7
		 WHERE $8 IS NULL OR notes.updated_at IS NULL OR notes.updated_at <= $8`,
		[path, nameFor(path), pinned === undefined ? null : pinned ? 1 : 0, content, revision, JSON.stringify(meta ?? {}), now, unlessUpdatedAfter ?? null]
	);
	return result.rowsAffected > 0;
}

/** Record a local edit that has not reached the server. base_* is untouched —
 * it stays the merge ancestor until a push succeeds. */
export async function writeDirty(path: string, content: string, revision: string): Promise<void> {
	await must().execute(
		`UPDATE notes SET content = $2, revision = $3, updated_at = $4,
		   sync_status = CASE WHEN sync_status = 'new' THEN 'new' ELSE 'dirty' END
		 WHERE path = $1`,
		[path, content, revision, Date.now()]
	);
}

export async function insertNew(path: string, content: string, revision: string): Promise<void> {
	await must().execute(
		`INSERT INTO notes (path, name, pinned, content, revision, base_content, base_revision,
		                    meta_json, sync_status, fetched_at, updated_at)
		 VALUES ($1, $2, 0, $3, $4, '', NULL, '{}', 'new', NULL, $5)
		 ON CONFLICT(path) DO UPDATE SET
		   content = $3, revision = $4, sync_status = 'new', updated_at = $5`,
		[path, nameFor(path), content, revision, Date.now()]
	);
}

/** A push of this row's text landed, but adoptServer left the row because it
 * was written again while the push was out. The server now holds that text as
 * `baseContent` at `baseRevision`, and the newer local text is an edit of it:
 * make it the merge ancestor, and a 'new' row an edit of the note the server
 * has now. A base that moved meanwhile (a live event merged the row onto a
 * newer server copy) is not `fromRevision` and is left alone. */
export async function rebasePending(
	path: string,
	baseContent: string,
	baseRevision: string,
	fromRevision: string | null
): Promise<void> {
	await must().execute(
		`UPDATE notes SET base_content = $2, base_revision = $3, sync_status = 'dirty'
		 WHERE path = $1 AND sync_status IN ('dirty', 'new') AND base_revision IS $4`,
		[path, baseContent, baseRevision, fromRevision]
	);
}

/** After a push landed or the server changed base: move the merge ancestor. */
export async function setBase(
	path: string,
	baseContent: string,
	baseRevision: string
): Promise<void> {
	await must().execute(
		'UPDATE notes SET base_content = $2, base_revision = $3 WHERE path = $1',
		[path, baseContent, baseRevision]
	);
}

export async function setPinned(path: string, pinned: boolean): Promise<void> {
	await must().execute('UPDATE notes SET pinned = $2 WHERE path = $1', [
		path,
		pinned ? 1 : 0
	]);
}

export async function markDeleted(path: string): Promise<void> {
	await must().execute("UPDATE notes SET sync_status = 'deleted' WHERE path = $1", [path]);
}

export async function removeNote(path: string): Promise<void> {
	await must().execute('DELETE FROM notes WHERE path = $1', [path]);
}

export async function moveNote(path: string, newPath: string): Promise<void> {
	await must().execute(
		'UPDATE notes SET path = $2, name = $3, updated_at = $4 WHERE path = $1',
		[path, newPath, nameFor(newPath), Date.now()]
	);
}

/** Rows with local changes awaiting a push. `new` sorts before `dirty` so
 * creations exist server-side before edits to them replay. */
export async function pendingNotes(): Promise<NoteRow[]> {
	return must().select<NoteRow[]>(
		"SELECT * FROM notes WHERE sync_status IN ('dirty', 'new') ORDER BY updated_at"
	);
}

export async function pendingCount(): Promise<number> {
	const rows = await must().select<{ n: number }[]>(
		`SELECT (SELECT COUNT(*) FROM notes WHERE sync_status IN ('dirty','new')) +
		        (SELECT COUNT(*) FROM ops) AS n`
	);
	return rows[0]?.n ?? 0;
}

// --- folders ----------------------------------------------------------------

export async function allFolders(): Promise<string[]> {
	const rows = await must().select<{ path: string }[]>('SELECT path FROM folders ORDER BY path');
	return rows.map((r) => r.path);
}

export async function replaceFolders(paths: string[]): Promise<void> {
	const d = must();
	await d.execute('DELETE FROM folders');
	for (const p of paths) {
		await d.execute('INSERT OR IGNORE INTO folders (path) VALUES ($1)', [p]);
	}
}

export async function addFolder(path: string): Promise<void> {
	await must().execute('INSERT OR IGNORE INTO folders (path) VALUES ($1)', [path]);
}

// "Inside folder $1": an exact, case-sensitive prefix test. LIKE would treat
// `_` as a wildcard and fold ASCII case, reaching into `my-notes/` from
// `my_notes` or `work/` from `Work` — distinct folders on the Linux vault.
const UNDER = "substr(path, 1, length($1) + 1) = $1 || '/'";

export async function removeFolderTree(path: string): Promise<void> {
	const d = must();
	await d.execute(`DELETE FROM folders WHERE path = $1 OR ${UNDER}`, [path]);
	// Unsynced rows go too: folder delete is online-only, so the server has already trashed them.
	await d.execute(`DELETE FROM notes WHERE ${UNDER}`, [path]);
}

/** One statement per table, so a path collision aborts the whole notes move
 * instead of stranding half the tree (the plugin's connection pool gives no
 * transaction across execute calls). A note's name is its last segment, which
 * a folder rename never changes. Folder rows are bare paths: a destination
 * already in the cache is merged into rather than failing after the notes
 * have moved. */
export async function renameFolderTree(path: string, newPath: string): Promise<void> {
	const d = must();
	await d.execute(
		`UPDATE notes SET path = $2 || substr(path, length($1) + 1), updated_at = $3
		 WHERE ${UNDER}`,
		[path, newPath, Date.now()]
	);
	await d.execute(
		`UPDATE OR REPLACE folders SET path = $2 || substr(path, length($1) + 1)
		 WHERE path = $1 OR ${UNDER}`,
		[path, newPath]
	);
}

// --- ops queue --------------------------------------------------------------

export async function enqueueOp(
	op: OpKind,
	path: string,
	newPath: string | null = null,
	payload: Record<string, unknown> | null = null
): Promise<void> {
	await must().execute(
		'INSERT INTO ops (op, path, new_path, payload_json, created_at) VALUES ($1, $2, $3, $4, $5)',
		[op, path, newPath, payload ? JSON.stringify(payload) : null, Date.now()]
	);
}

export async function listOps(): Promise<OpRow[]> {
	return must().select<OpRow[]>('SELECT * FROM ops ORDER BY id');
}

export async function deleteOp(id: number): Promise<void> {
	await must().execute('DELETE FROM ops WHERE id = $1', [id]);
}

/** Keep an op's latest error. Only `counted` failures (the server refused the
 * op itself) add to the attempts that decide when it is dropped. */
export async function recordOpFailure(id: number, error: string, counted = true): Promise<number> {
	await must().execute(
		'UPDATE ops SET attempts = attempts + $3, last_error = $2 WHERE id = $1',
		[id, error.slice(0, 500), counted ? 1 : 0]
	);
	const rows = await must().select<{ attempts: number }[]>(
		'SELECT attempts FROM ops WHERE id = $1',
		[id]
	);
	return rows[0]?.attempts ?? 0;
}

/** A note that exists only locally (`new`) was renamed before it could
 * replay — point its queued create/pin at where the row lives now. Called
 * after moveNote, so the row is already at `newPath`. Only ops queued since
 * the last rename into or out of `path` belong to this row; earlier ones are
 * the history of another note that lived there and replay as written. A row
 * the server already has is left alone: the caller queues a rename op, and
 * FIFO replay carries every earlier op to the new path in order. */
export async function rewriteOpPaths(path: string, newPath: string): Promise<void> {
	await must().execute(
		`UPDATE ops SET path = $2
		 WHERE path = $1 AND op IN ('create', 'pin')
		   AND id > COALESCE(
		     (SELECT MAX(id) FROM ops WHERE op = 'rename' AND (path = $1 OR new_path = $1)), 0)
		   AND EXISTS (SELECT 1 FROM notes WHERE path = $2 AND sync_status = 'new')`,
		[path, newPath]
	);
}

// --- conflicts --------------------------------------------------------------

export async function recordConflict(path: string, conflictPath: string): Promise<void> {
	await must().execute(
		'INSERT INTO conflicts (path, conflict_path, created_at) VALUES ($1, $2, $3)',
		[path, conflictPath, Date.now()]
	);
}

export async function unseenConflicts(): Promise<ConflictRow[]> {
	return must().select<ConflictRow[]>('SELECT * FROM conflicts WHERE seen = 0 ORDER BY id');
}

export async function markConflictSeen(id: number): Promise<void> {
	await must().execute('UPDATE conflicts SET seen = 1 WHERE id = $1', [id]);
}

/** The most recent conflicts, newest first, seen or not. */
export async function listConflicts(limit = 20): Promise<ConflictRow[]> {
	return must().select<ConflictRow[]>(
		'SELECT * FROM conflicts ORDER BY created_at DESC, id DESC LIMIT $1',
		[limit]
	);
}

export async function debugNote(message: string): Promise<void> {
	await must().execute(
		"INSERT INTO sync_meta (key, value) VALUES ('last_error', $1) ON CONFLICT(key) DO UPDATE SET value = $1",
		[message]
	);
}
