// Desktop only: the local note cache and offline write queue, in SQLite via
// tauri-plugin-sql. This is the durable copy — webview storage is treated as
// disposable, unsynced offline edits are not.
//
// Content edits are row state (`sync_status = 'dirty'`, with `base_*` as the
// three-way-merge ancestor); only structural operations (create, delete,
// rename, pin, folders) go through the FIFO `ops` queue, so compositions like
// create → edit → rename replay in the right order.

import Database from '@tauri-apps/plugin-sql';

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

let db: Database | null = null;

export async function openSyncDb(): Promise<void> {
	if (db) return;
	db = await Database.load('sqlite:exscalia.db');
	for (const stmt of SCHEMA.split(';')) {
		if (stmt.trim()) await db.execute(stmt);
	}
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
 * replay success): content and base move together, status becomes synced. */
export async function adoptServer(
	path: string,
	content: string,
	revision: string,
	meta: Record<string, unknown>,
	pinned?: boolean
): Promise<void> {
	const now = Date.now();
	await must().execute(
		`INSERT INTO notes (path, name, pinned, content, revision, base_content, base_revision,
		                    meta_json, sync_status, fetched_at, updated_at)
		 VALUES ($1, $2, COALESCE($3, 0), $4, $5, $4, $5, $6, 'synced', $7, $7)
		 ON CONFLICT(path) DO UPDATE SET
		   name = $2,
		   pinned = COALESCE($3, notes.pinned),
		   content = $4, revision = $5, base_content = $4, base_revision = $5,
		   meta_json = $6, sync_status = 'synced', fetched_at = $7, updated_at = $7`,
		[path, nameFor(path), pinned === undefined ? null : pinned ? 1 : 0, content, revision, JSON.stringify(meta ?? {}), now]
	);
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

export async function removeFolderTree(path: string): Promise<void> {
	const d = must();
	await d.execute('DELETE FROM folders WHERE path = $1 OR path LIKE $2', [path, `${path}/%`]);
	await d.execute('DELETE FROM notes WHERE path LIKE $1', [`${path}/%`]);
}

export async function renameFolderTree(path: string, newPath: string): Promise<void> {
	const d = must();
	const notes = await d.select<{ path: string }[]>('SELECT path FROM notes WHERE path LIKE $1', [
		`${path}/%`
	]);
	for (const row of notes) {
		await moveNote(row.path, newPath + row.path.slice(path.length));
	}
	const folders = await d.select<{ path: string }[]>(
		'SELECT path FROM folders WHERE path = $1 OR path LIKE $2',
		[path, `${path}/%`]
	);
	for (const row of folders) {
		await d.execute('UPDATE folders SET path = $2 WHERE path = $1', [
			row.path,
			newPath + row.path.slice(path.length)
		]);
	}
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

export async function recordOpFailure(id: number, error: string): Promise<number> {
	await must().execute('UPDATE ops SET attempts = attempts + 1, last_error = $2 WHERE id = $1', [
		id,
		error.slice(0, 500)
	]);
	const rows = await must().select<{ attempts: number }[]>(
		'SELECT attempts FROM ops WHERE id = $1',
		[id]
	);
	return rows[0]?.attempts ?? 0;
}

/** A queued note was renamed before it could replay — keep the queue pointing
 * at where the note lives now. */
export async function rewriteOpPaths(path: string, newPath: string): Promise<void> {
	await must().execute('UPDATE ops SET path = $2 WHERE path = $1', [path, newPath]);
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

export async function debugNote(message: string): Promise<void> {
	await must().execute(
		"INSERT INTO sync_meta (key, value) VALUES ('last_error', $1) ON CONFLICT(key) DO UPDATE SET value = $1",
		[message]
	);
}
