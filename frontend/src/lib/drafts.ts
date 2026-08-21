export interface LocalDraft {
	path: string;
	content: string;
	baseRevision: string;
	updatedAt: number;
}

const DB_NAME = 'exscalia';
const STORE = 'drafts';
const VERSION = 1;
let mutationQueue: Promise<void> = Promise.resolve();

function openDatabase(): Promise<IDBDatabase> {
	return new Promise((resolve, reject) => {
		const request = indexedDB.open(DB_NAME, VERSION);
		request.onupgradeneeded = () => {
			if (!request.result.objectStoreNames.contains(STORE)) {
				request.result.createObjectStore(STORE, { keyPath: 'path' });
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

export async function readDraft(path: string): Promise<LocalDraft | null> {
	if (typeof indexedDB === 'undefined') return null;
	try {
		await mutationQueue.catch(() => {});
		return (await transact<LocalDraft | undefined>('readonly', (store) => store.get(path))) ?? null;
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

export function removeDraft(path: string): Promise<void> {
	if (typeof indexedDB === 'undefined') return Promise.resolve();
	return enqueueMutation(async () => {
		try {
			await transact<undefined>('readwrite', (store) => store.delete(path));
		} catch {
			// A stale recovery draft is preferable to disrupting the editor.
		}
	});
}

export async function moveDraft(oldPath: string, newPath: string): Promise<void> {
	const draft = await readDraft(oldPath);
	if (!draft) return;
	await writeDraft({ ...draft, path: newPath });
	await removeDraft(oldPath);
}
