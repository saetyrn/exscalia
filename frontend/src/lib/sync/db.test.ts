// The desktop cache's SQL, run as written: tauri-plugin-sql is swapped for an
// in-memory node:sqlite database, so every statement in db.ts executes against
// a real SQLite engine. The folder and op-queue tests pin the path predicates
// that decide which unsynced rows a folder operation or an offline rename may
// touch.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_SERVER } from '../runtime';

const { FakeDatabase, files, loaded, closed } = vi.hoisted(() => {
	interface Statement {
		run(params: object): { changes: number | bigint; lastInsertRowid: number | bigint };
		all(params: object): unknown[];
	}
	interface Sqlite {
		prepare(sql: string): Statement;
	}

	/** db.ts numbers its parameters `$1..$n` and reuses them out of order
	 * (writeDirty binds `$1` last, adoptServer repeats `$4` and `$7`). sqlx
	 * binds such parameters by the number in the name, so do the same. */
	function named(sql: string, params: unknown[] = []): Record<string, unknown> {
		const out: Record<string, unknown> = {};
		for (const [, n] of sql.matchAll(/\$(\d+)/g)) {
			out[`$${n}`] = params[Number(n) - 1] ?? null;
		}
		return out;
	}

	/** One in-memory database per file name, so a name loaded again finds
	 * what was written under it, as the app-data file would. */
	const files = new Map<string, Sqlite>();
	/** Every name load() was asked for, and every name close() was given. */
	const loaded: string[] = [];
	const closed: (string | undefined)[] = [];

	class FakeDatabase {
		private open = true;

		constructor(
			readonly path: string,
			private readonly sqlite: Sqlite
		) {}

		static async load(path: string): Promise<FakeDatabase> {
			loaded.push(path);
			let sqlite = files.get(path);
			if (!sqlite) {
				// Kept out of the type checker's reach: the project carries no node
				// typings, and the module only has to exist at test time.
				const specifier = 'node:sqlite';
				const { DatabaseSync } = await import(/* @vite-ignore */ specifier);
				sqlite = new DatabaseSync(':memory:') as Sqlite;
				files.set(path, sqlite);
			}
			return new FakeDatabase(path, sqlite);
		}

		async close(path?: string): Promise<boolean> {
			closed.push(path);
			this.open = false;
			return true;
		}

		private conn(): Sqlite {
			if (!this.open) throw new Error(`${this.path} is closed`);
			return this.sqlite;
		}

		async execute(sql: string, params?: unknown[]) {
			const r = this.conn().prepare(sql).run(named(sql, params));
			return { rowsAffected: Number(r.changes), lastInsertId: Number(r.lastInsertRowid) };
		}

		async select<T>(sql: string, params?: unknown[]): Promise<T> {
			return this.conn().prepare(sql).all(named(sql, params)) as T;
		}
	}
	return { FakeDatabase, files, loaded, closed };
});

vi.mock('@tauri-apps/plugin-sql', () => ({ default: FakeDatabase }));

let db: typeof import('./db');

beforeEach(async () => {
	// A fresh module is a fresh connection and no file survives a test, so
	// each test starts from an empty cache.
	files.clear();
	loaded.length = 0;
	closed.length = 0;
	vi.resetModules();
	db = await import('./db');
	await db.openSyncDb(DEFAULT_SERVER);
});

async function paths(): Promise<string[]> {
	return (await db.allNotes()).map((n) => n.path);
}

async function seed(...notePaths: string[]): Promise<void> {
	for (const p of notePaths) await db.adoptServer(p, `# ${p}\n`, `rev-${p}`, {});
}

describe('folder predicates', () => {
	it('treats _ in a folder name literally', async () => {
		await seed('my_notes/a.md', 'my-notes/x.md', 'myXnotes/y.md');
		await db.replaceFolders(['my_notes', 'my-notes', 'myXnotes']);

		await db.renameFolderTree('my_notes', 'renamed');
		expect(await paths()).toEqual(['my-notes/x.md', 'myXnotes/y.md', 'renamed/a.md']);
		expect(await db.allFolders()).toEqual(['my-notes', 'myXnotes', 'renamed']);

		await db.removeFolderTree('renamed');
		expect(await paths()).toEqual(['my-notes/x.md', 'myXnotes/y.md']);
		expect(await db.allFolders()).toEqual(['my-notes', 'myXnotes']);
	});

	it('matches folder names case-sensitively, like the Linux vault', async () => {
		await seed('Work/a.md', 'work/x.md');
		await db.writeDirty('work/x.md', 'unsynced edit', 'rev-local');
		await db.replaceFolders(['Work', 'work']);

		await db.renameFolderTree('Work', 'Jobs');
		expect(await paths()).toEqual(['Jobs/a.md', 'work/x.md']);
		expect(await db.allFolders()).toEqual(['Jobs', 'work']);

		await db.removeFolderTree('Jobs');
		const left = await db.getNote('work/x.md');
		expect(left?.content).toBe('unsynced edit');
		expect(left?.sync_status).toBe('dirty');
		expect(await db.allFolders()).toEqual(['work']);
	});

	it('does not match a sibling that merely shares the prefix', async () => {
		await seed('a/x.md', 'ab/y.md', 'a.md');
		await db.replaceFolders(['a', 'ab']);

		await db.renameFolderTree('a', 'z');
		expect(await paths()).toEqual(['a.md', 'ab/y.md', 'z/x.md']);
		expect(await db.allFolders()).toEqual(['ab', 'z']);
	});

	it('moves a nested tree, keeping names and row state', async () => {
		await seed('a/b/c.md', 'a/top.md');
		await db.writeDirty('a/b/c.md', 'local', 'rev-local');
		await db.replaceFolders(['a', 'a/b']);

		await db.renameFolderTree('a', 'x');
		expect(await paths()).toEqual(['x/b/c.md', 'x/top.md']);
		expect(await db.allFolders()).toEqual(['x', 'x/b']);
		const moved = await db.getNote('x/b/c.md');
		expect(moved?.name).toBe('c');
		expect(moved?.content).toBe('local');
		expect(moved?.base_content).toBe('# a/b/c.md\n');
		expect(moved?.sync_status).toBe('dirty');
	});

	it('leaves the whole tree in place when one destination is taken', async () => {
		await seed('a/one.md', 'a/two.md', 'x/two.md');
		await db.replaceFolders(['a', 'x']);

		await expect(db.renameFolderTree('a', 'x')).rejects.toThrow();
		expect(await paths()).toEqual(['a/one.md', 'a/two.md', 'x/two.md']);
		expect(await db.allFolders()).toEqual(['a', 'x']);
	});

	it('merges into a destination folder the cache already lists', async () => {
		await seed('a/one.md', 'x/two.md');
		await db.replaceFolders(['a', 'a/sub', 'x']);

		await db.renameFolderTree('a', 'x');
		expect(await paths()).toEqual(['x/one.md', 'x/two.md']);
		expect(await db.allFolders()).toEqual(['x', 'x/sub']);
	});
});

describe('rewriteOpPaths', () => {
	/** What offline-api's renameNote fallback does once the network is gone. */
	async function offlineRename(path: string, newPath: string): Promise<void> {
		const row = await db.getNote(path);
		await db.moveNote(path, newPath);
		await db.rewriteOpPaths(path, newPath);
		if (row?.sync_status !== 'new') {
			await db.enqueueOp('rename', path, newPath, { new_title: null });
		}
	}

	async function queue(): Promise<string[]> {
		const ops = await db.listOps();
		return ops.map((o) => [o.op, o.path, o.new_path].filter(Boolean).join(' '));
	}

	it('does not rewrite the history of an earlier note at the same path', async () => {
		await seed('A.md');
		await db.enqueueOp('pin', 'A.md', null, { pinned: true });
		await offlineRename('A.md', 'B.md');
		await db.insertNew('A.md', 'second A', 'rev-2');
		await db.enqueueOp('create', 'A.md');
		await offlineRename('A.md', 'C.md');

		expect(await queue()).toEqual(['pin A.md', 'rename A.md B.md', 'create C.md']);
	});

	it('leaves a queued pin on a synced note for the rename op to carry', async () => {
		await seed('A.md');
		await db.setPinned('A.md', true);
		await db.enqueueOp('pin', 'A.md', null, { pinned: true });
		await offlineRename('A.md', 'B.md');

		expect(await queue()).toEqual(['pin A.md', 'rename A.md B.md']);
	});

	it('follows a note that only exists locally to its new path', async () => {
		await db.insertNew('A.md', 'draft', 'rev-1');
		await db.enqueueOp('create', 'A.md');
		await db.enqueueOp('pin', 'A.md', null, { pinned: true });
		await offlineRename('A.md', 'B.md');
		await offlineRename('B.md', 'C.md');

		expect(await queue()).toEqual(['create C.md', 'pin C.md']);
	});

	it('keeps a queued delete of the old note when a new one takes its path', async () => {
		await seed('X.md');
		await offlineRename('X.md', 'A.md');
		await db.markDeleted('A.md');
		await db.enqueueOp('delete', 'A.md');
		await db.insertNew('A.md', 'fresh', 'rev-3');
		await db.enqueueOp('create', 'A.md');
		await offlineRename('A.md', 'C.md');

		expect(await queue()).toEqual(['rename X.md A.md', 'delete A.md', 'create C.md']);
	});
});

describe('adoptServer', () => {
	it('leaves a row written after the request started', async () => {
		await seed('a.md');
		await db.writeDirty('a.md', 'typed while the save was out', 'rev-local');
		const writtenAt = (await db.getNote('a.md'))!.updated_at!;

		await db.adoptServer('a.md', 'server answer', 'rev-2', {}, undefined, writtenAt - 1);

		expect(await db.getNote('a.md')).toMatchObject({
			content: 'typed while the save was out',
			revision: 'rev-local',
			base_content: '# a.md\n',
			base_revision: 'rev-a.md',
			sync_status: 'dirty',
			updated_at: writtenAt
		});
	});

	it('adopts a row last written before the request started', async () => {
		await seed('a.md');
		await db.writeDirty('a.md', 'fallback save', 'rev-local');
		const writtenAt = (await db.getNote('a.md'))!.updated_at!;

		await db.adoptServer('a.md', 'fallback save, then more', 'rev-2', {}, undefined, writtenAt);

		expect(await db.getNote('a.md')).toMatchObject({
			content: 'fallback save, then more',
			base_revision: 'rev-2',
			sync_status: 'synced'
		});
	});

	it('adopts unconditionally without a start time, and inserts a missing row', async () => {
		await seed('a.md');
		await db.writeDirty('a.md', 'local', 'rev-local');
		await db.adoptServer('a.md', 'server', 'rev-2', {});
		await db.adoptServer('b.md', 'fresh', 'rev-b', {}, undefined, 0);

		expect(await db.getNote('a.md')).toMatchObject({ content: 'server', sync_status: 'synced' });
		expect(await db.getNote('b.md')).toMatchObject({ content: 'fresh', sync_status: 'synced' });
	});
});

describe('adoptServer result', () => {
	it('says whether the row was adopted', async () => {
		await seed('a.md');
		await db.writeDirty('a.md', 'typed while the push was out', 'rev-local');
		const writtenAt = (await db.getNote('a.md'))!.updated_at!;

		expect(await db.adoptServer('a.md', 'answer', 'rev-2', {}, undefined, writtenAt - 1)).toBe(false);
		expect(await db.adoptServer('a.md', 'answer', 'rev-2', {}, undefined, writtenAt)).toBe(true);
		expect(await db.adoptServer('b.md', 'fresh', 'rev-b', {})).toBe(true);
	});
});

describe('rebasePending', () => {
	it('puts the text a push left under a pending row, and makes a new row an edit', async () => {
		await seed('a.md');
		await db.writeDirty('a.md', 'newer', 'rev-newer');
		await db.insertNew('n.md', 'newer', 'rev-local');

		await db.rebasePending('a.md', 'pushed', 'rev-pushed', 'rev-a.md');
		await db.rebasePending('n.md', 'created', 'rev-created', null);

		expect(await db.getNote('a.md')).toMatchObject({
			content: 'newer',
			base_content: 'pushed',
			base_revision: 'rev-pushed',
			sync_status: 'dirty'
		});
		expect(await db.getNote('n.md')).toMatchObject({
			content: 'newer',
			base_content: 'created',
			base_revision: 'rev-created',
			sync_status: 'dirty'
		});
	});

	it('leaves a base that moved meanwhile, and a synced row', async () => {
		await seed('a.md', 'b.md');
		// A live event merged the row onto a newer server copy.
		await db.setBase('a.md', 'newer server', 'rev-3');
		await db.writeDirty('a.md', 'merged', 'rev-merged');

		await db.rebasePending('a.md', 'pushed', 'rev-pushed', 'rev-a.md');
		await db.rebasePending('b.md', 'pushed', 'rev-pushed', 'rev-b.md');

		expect(await db.getNote('a.md')).toMatchObject({
			base_content: 'newer server',
			base_revision: 'rev-3',
			sync_status: 'dirty'
		});
		expect(await db.getNote('b.md')).toMatchObject({
			base_content: '# b.md\n',
			base_revision: 'rev-b.md',
			sync_status: 'synced'
		});
	});
});

describe('noteHeads', () => {
	it('lists live rows without their bodies', async () => {
		await seed('b.md', 'a.md', 'gone.md');
		await db.writeDirty('b.md', 'edited', 'rev-local');
		await db.setPinned('a.md', true);
		await db.markDeleted('gone.md');

		expect(await db.noteHeads()).toEqual([
			{
				path: 'a.md',
				name: 'a',
				pinned: 1,
				base_revision: 'rev-a.md',
				sync_status: 'synced',
				meta_json: '{}'
			},
			{
				path: 'b.md',
				name: 'b',
				pinned: 0,
				base_revision: 'rev-b.md',
				sync_status: 'dirty',
				meta_json: '{}'
			}
		]);
	});
});

describe('openSyncDb', () => {
	const DEV = 'http://localhost:8600';

	async function queue(): Promise<string[]> {
		return (await db.listOps()).map((o) => `${o.op} ${o.path}`);
	}

	it('keeps the original file for the default server', () => {
		expect(loaded).toEqual(['sqlite:exscalia.db']);
	});

	it('gives another server its own file, named the same every time', async () => {
		await db.openSyncDb(DEV);
		vi.resetModules();
		db = await import('./db');
		await db.openSyncDb(`${DEV}/`);

		expect(loaded).toEqual([
			'sqlite:exscalia.db',
			'sqlite:exscalia-42311b6467f3.db',
			'sqlite:exscalia-42311b6467f3.db'
		]);
	});

	it('does not reopen for the server already open', async () => {
		await db.openSyncDb(DEFAULT_SERVER);
		await db.openSyncDb(`${DEFAULT_SERVER}/`);

		expect(loaded).toEqual(['sqlite:exscalia.db']);
		expect(closed).toEqual([]);
	});

	it('keeps the queue and rows of each server in its own file', async () => {
		await seed('a.md');
		await db.markDeleted('a.md');
		await db.enqueueOp('delete', 'a.md');

		await db.openSyncDb(DEV);
		expect(closed).toEqual(['sqlite:exscalia.db']);
		expect(await queue()).toEqual([]);
		expect(await db.pendingCount()).toBe(0);
		await db.insertNew('drill.md', 'planted', 'rev-drill');
		await db.enqueueOp('create', 'drill.md');

		await db.openSyncDb(DEFAULT_SERVER);
		expect(closed).toEqual(['sqlite:exscalia.db', 'sqlite:exscalia-42311b6467f3.db']);
		expect(await queue()).toEqual(['delete a.md']);
		expect(await db.getNote('drill.md')).toBeNull();

		await db.openSyncDb(DEV);
		expect(await queue()).toEqual(['create drill.md']);
	});

	it('leaves the last server asked for open when switches overlap', async () => {
		await Promise.all([db.openSyncDb(DEV), db.openSyncDb('https://other.example')]);
		await db.insertNew('x.md', 'here', 'rev-x');

		expect(loaded.slice(1)).toEqual([
			'sqlite:exscalia-42311b6467f3.db',
			expect.stringMatching(/^sqlite:exscalia-[0-9a-f]{12}\.db$/)
		]);
		expect(closed).toEqual(['sqlite:exscalia.db', 'sqlite:exscalia-42311b6467f3.db']);
		expect(await db.getNote('x.md')).not.toBeNull();
		await db.openSyncDb(DEV);
		expect(await db.getNote('x.md')).toBeNull();
	});
});

describe('schema version', () => {
	const FILE = 'sqlite:exscalia.db';

	function pragma(sql: string): unknown[] {
		return files.get(FILE)!.prepare(sql).all({});
	}

	function userVersion(): number {
		return (pragma('PRAGMA user_version')[0] as { user_version: number }).user_version;
	}

	function tables(): string[] {
		return (pragma("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name") as {
			name: string;
		}[]).map((t) => t.name);
	}

	/** Quit and launch again: a fresh module opening the same file. */
	async function reopen(): Promise<void> {
		vi.resetModules();
		db = await import('./db');
		await db.openSyncDb(DEFAULT_SERVER);
	}

	it('stamps a fresh file with the current version', () => {
		expect(userVersion()).toBe(db.SCHEMA_VERSION);
		expect(db.isOpen()).toBe(true);
	});

	it('adopts a file from before versioning without touching its rows', async () => {
		await seed('a.md');
		await db.writeDirty('a.md', 'unsynced', 'rev-local');
		await db.enqueueOp('rename', 'a.md', 'b.md');
		await db.recordConflict('a.md', 'a (conflict).md');
		// What every install made before versioning holds: the same tables,
		// user_version never set.
		pragma('PRAGMA user_version = 0');
		expect(userVersion()).toBe(0);

		await reopen();

		expect(userVersion()).toBe(db.SCHEMA_VERSION);
		expect(await db.getNote('a.md')).toMatchObject({
			content: 'unsynced',
			base_content: '# a.md\n',
			sync_status: 'dirty'
		});
		expect(await db.listOps()).toMatchObject([{ op: 'rename', path: 'a.md', new_path: 'b.md' }]);
		expect(await db.listConflicts()).toMatchObject([{ path: 'a.md', seen: 0 }]);
	});

	it('refuses a file from a newer build and leaves it as it is', async () => {
		await seed('a.md');
		pragma('DROP TABLE folders');
		pragma(`PRAGMA user_version = ${db.SCHEMA_VERSION + 1}`);

		vi.resetModules();
		db = await import('./db');
		await expect(db.openSyncDb(DEFAULT_SERVER)).rejects.toThrow(
			'sync cache schema is newer than this app'
		);

		expect(db.isOpen()).toBe(false);
		expect(closed).toEqual([FILE]);
		await expect(db.getNote('a.md')).rejects.toThrow('sync db not opened');
		expect(userVersion()).toBe(db.SCHEMA_VERSION + 1);
		expect(tables()).not.toContain('folders');

		// Another server's cache still opens.
		await db.openSyncDb('http://localhost:8600');
		expect(db.isOpen()).toBe(true);
	});
});

describe('listConflicts', () => {
	it('lists conflicts newest first, seen or not', async () => {
		const now = vi.spyOn(Date, 'now');
		now.mockReturnValue(1_000);
		await db.recordConflict('a.md', 'a (conflict).md');
		now.mockReturnValue(3_000);
		await db.recordConflict('b.md', 'b (conflict).md');
		now.mockReturnValue(2_000);
		await db.recordConflict('c.md', 'c (conflict).md');
		await db.recordConflict('d.md', 'd (conflict).md');
		now.mockRestore();
		const [first] = await db.unseenConflicts();
		await db.markConflictSeen(first.id);

		expect((await db.listConflicts()).map((c) => [c.path, c.conflict_path, c.seen])).toEqual([
			['b.md', 'b (conflict).md', 0],
			['d.md', 'd (conflict).md', 0],
			['c.md', 'c (conflict).md', 0],
			['a.md', 'a (conflict).md', 1]
		]);
		expect((await db.listConflicts(2)).map((c) => c.path)).toEqual(['b.md', 'd.md']);
	});
});

describe('recordOpFailure', () => {
	it('keeps every error but counts only the ones that say so', async () => {
		await db.enqueueOp('create_folder', 'Projects');
		const [op] = await db.listOps();

		expect(await db.recordOpFailure(op.id, 'Internal Server Error', false)).toBe(0);
		expect(await db.listOps()).toMatchObject([
			{ attempts: 0, last_error: 'Internal Server Error' }
		]);
		expect(await db.recordOpFailure(op.id, 'Invalid folder path')).toBe(1);
		expect(await db.listOps()).toMatchObject([{ attempts: 1, last_error: 'Invalid folder path' }]);
	});
});
