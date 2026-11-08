export interface LocalDraft {
	path: string;
	/** The tab that wrote it (`CLIENT_ID`). Drafts are keyed by path AND owner:
	 * keyed by path alone, another tab's keystrokes overwrote a closed tab's
	 * crash draft and its next save deleted it. '' marks a draft from before. */
	owner: string;
	content: string;
	/** The server revision the draft was edited on, and that revision's text —
	 * the common ancestor recovery merges from once the note has moved on.
	 * Drafts from before v2 have no baseContent. */
	baseRevision: string;
	baseContent?: string;
	updatedAt: number;
}

const DB_NAME = 'exscalia';
const STORE = 'drafts';
const VERSION = 2;
let mutationQueue: Promise<void> = Promise.resolve();

function createStore(db: IDBDatabase): IDBObjectStore {
	const store = db.createObjectStore(STORE, { keyPath: ['path', 'owner'] });
	store.createIndex('path', 'path');
	return store;
}

function openDatabase(): Promise<IDBDatabase> {
	return new Promise((resolve, reject) => {
		const request = indexedDB.open(DB_NAME, VERSION);
		request.onupgradeneeded = (event) => {
			const db = request.result;
			if (!db.objectStoreNames.contains(STORE)) {
				createStore(db);
				return;
			}
			if (event.oldVersion < 2) {
				// v1 keyed drafts by path alone. Rebuild the store with the new key
				// and carry every row over as owned by no tab, all on the upgrade
				// transaction: no tab's own removeDraft matches them, and the next
				// open of the note recovers them like any draft a closed tab left.
				const rows = request.transaction!.objectStore(STORE).getAll();
				rows.onsuccess = () => {
					db.deleteObjectStore(STORE);
					const store = createStore(db);
					for (const row of rows.result) store.put({ ...row, owner: '' });
				};
			}
		};
		request.onsuccess = () => {
			const db = request.result;
			// Drop the cached handle if this connection goes away, so the next
			// call reopens instead of reusing a dead one.
			const forget = () => {
				if (live === db) {
					live = null;
					connection = null;
				}
			};
			db.onclose = forget;
			db.onversionchange = () => {
				db.close();
				forget();
			};
			live = db;
			resolve(db);
		};
		request.onerror = () => reject(request.error);
	});
}

/** One connection for the tab's lifetime.
 *
 * This used to open and close the database on every single call, and the note
 * page writes a draft on every keystroke — a full connection handshake per
 * character typed. */
let connection: Promise<IDBDatabase> | null = null;
let live: IDBDatabase | null = null;

function database(): Promise<IDBDatabase> {
	if (!connection) {
		connection = openDatabase().catch((error) => {
			connection = null; // a failed open must not be cached
			throw error;
		});
	}
	return connection;
}

async function transact<T>(
	mode: IDBTransactionMode,
	run: (store: IDBObjectStore) => IDBRequest<T>
): Promise<T> {
	const db = await database();
	return await new Promise<T>((resolve, reject) => {
		const transaction = db.transaction(STORE, mode);
		const request = run(transaction.objectStore(STORE));
		request.onsuccess = () => resolve(request.result);
		request.onerror = () => reject(request.error);
	});
}

/** Several requests in one transaction, settled once it commits. */
async function transactAll(run: (store: IDBObjectStore) => void): Promise<void> {
	const db = await database();
	return await new Promise<void>((resolve, reject) => {
		const transaction = db.transaction(STORE, 'readwrite');
		transaction.oncomplete = () => resolve();
		transaction.onabort = () => reject(transaction.error);
		run(transaction.objectStore(STORE));
	});
}

/** The newest draft for `path` from any tab: this one's, or one a closed or
 * crashed tab left behind. `owner` says whose it is. */
export async function readDraft(path: string): Promise<LocalDraft | null> {
	if (typeof indexedDB === 'undefined') return null;
	try {
		await mutationQueue.catch(() => {});
		const drafts = await transact<LocalDraft[]>('readonly', (store) =>
			store.index('path').getAll(path)
		);
		let newest: LocalDraft | null = null;
		for (const draft of drafts) if (!newest || draft.updatedAt > newest.updatedAt) newest = draft;
		return newest;
	} catch {
		return null;
	}
}

function enqueueMutation(operation: () => Promise<void>): Promise<void> {
	const next = mutationQueue.catch(() => {}).then(operation);
	mutationQueue = next;
	return next;
}

export function writeDraft(draft: LocalDraft): Promise<void> {
	if (typeof indexedDB === 'undefined') return Promise.resolve();
	return enqueueMutation(async () => {
		try {
			await transact<IDBValidKey>('readwrite', (store) => store.put(draft));
		} catch {
			// Editing must remain available even if private browsing blocks storage.
		}
	});
}

/** Remove one tab's draft for `path`; other tabs' drafts stay. */
export function removeDraft(path: string, owner: string): Promise<void> {
	if (typeof indexedDB === 'undefined') return Promise.resolve();
	return enqueueMutation(async () => {
		try {
			await transact<undefined>('readwrite', (store) => store.delete([path, owner]));
		} catch {
			// A stale recovery draft is preferable to disrupting the editor.
		}
	});
}

/** Remove every tab's draft for `path` — the note itself is gone. */
export function removeAllDrafts(path: string): Promise<void> {
	if (typeof indexedDB === 'undefined') return Promise.resolve();
	return enqueueMutation(async () => {
		try {
			// Every [path, owner] key: a one-element array sorts before all its
			// extensions, and an array after any string owner.
			await transact<undefined>('readwrite', (store) =>
				store.delete(IDBKeyRange.bound([path], [path, []]))
			);
		} catch {
			// A stale recovery draft is preferable to disrupting the editor.
		}
	});
}

/** Carry drafts to the note's new path: every tab's on a rename. `owner`
 * moves only that tab's, and `dropBase` sends it without its base — for a
 * conflict copy, a new note that base never described, so the next open
 * recovers the draft as written. */
export function moveDraft(
	oldPath: string,
	newPath: string,
	options: { owner?: string; dropBase?: boolean } = {}
): Promise<void> {
	if (typeof indexedDB === 'undefined' || oldPath === newPath) return Promise.resolve();
	return enqueueMutation(async () => {
		try {
			await transactAll((store) => {
				const rows = store.index('path').getAll(oldPath);
				rows.onsuccess = () => {
					for (const row of rows.result as LocalDraft[]) {
						if (options.owner !== undefined && row.owner !== options.owner) continue;
						store.delete([row.path, row.owner]);
						const moved: LocalDraft = { ...row, path: newPath };
						if (options.dropBase) {
							moved.baseRevision = '';
							delete moved.baseContent;
						}
						store.put(moved);
					}
				};
			});
		} catch {
			// A draft left at the old path is recovered from there instead.
		}
	});
}
