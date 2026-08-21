import {
	api,
	type GraphicSpec,
	type MediaGallery,
	type NoteListItem,
	type OmniaAction,
	type OmniaConfirmation,
	type OmniaThread,
	type TagCount,
	type ThreadSummary
} from './api';

/** Desktop sync engine status. On the web this stays at its initial value
 * ('live' — everything is always the server). */
export const syncState = $state({
	mode: 'live' as 'offline' | 'connecting' | 'replaying' | 'live',
	/** Local changes waiting to reach the server. */
	pending: 0,
	progressDone: 0,
	progressTotal: 0
});

export const vault = $state({
	notes: [] as NoteListItem[],
	folders: [] as string[],
	tags: [] as TagCount[],
	/** Sidebar folder arrangement, newest-first by intent rather than by name.
	 * Folders missing from this list sort alphabetically after the ones in it. */
	folderOrder: [] as string[],
	noteOrder: [] as string[],
	pendingCount: 0,
	loaded: false
});

/** Most-recently-opened notes, newest-first and deduped. Persisted under
 * `exscalia.recentNotes` and shared by the command palette and the mobile home
 * shortcuts — the note page calls rememberNote() whenever a note opens, so this
 * is the single writer. */
const RECENT_LIMIT = 8;
export const recentNotes = $state({ paths: readRecentNotes() });

function readRecentNotes(): string[] {
	if (typeof localStorage === 'undefined') return [];
	try {
		const raw = JSON.parse(localStorage.getItem('exscalia.recentNotes') ?? '[]');
		return Array.isArray(raw) ? raw.filter((p): p is string => typeof p === 'string') : [];
	} catch {
		return [];
	}
}

export function rememberNote(path: string) {
	if (!path || recentNotes.paths[0] === path) return;
	recentNotes.paths = [path, ...recentNotes.paths.filter((p) => p !== path)].slice(0, RECENT_LIMIT);
	try {
		localStorage.setItem('exscalia.recentNotes', JSON.stringify(recentNotes.paths));
	} catch {
		/* private mode — the jump-back shortcuts just won't survive a reload */
	}
}

export async function refreshPendingCount() {
	try {
		vault.pendingCount = (await api.pendingCount()).count;
	} catch {
		/* backend not ready */
	}
}

export async function refreshNotes() {
	[vault.notes, vault.folders] = await Promise.all([api.listNotes(), api.listFolders()]);
	vault.loaded = true;
}

/** Read the saved folder arrangement. Separate from refreshNotes because it
 * only changes when you rearrange it, not every time the note list moves. */
export async function refreshFolderOrder() {
	try {
		vault.folderOrder = (await api.folderOrder()).order;
	} catch {
		/* Falls back to alphabetical, which is the pre-existing behaviour. */
	}
}

/** Persist the arrangement. The full list is sent so entries for folders that
 * have since been renamed or deleted are dropped server-side. */
export async function saveFolderOrder(order: string[]) {
	vault.folderOrder = order;
	try {
		vault.folderOrder = (await api.saveFolderOrder(order)).order;
	} catch (cause) {
		await refreshFolderOrder();
		throw cause;
	}
}

export async function refreshNoteOrder() {
	try {
		vault.noteOrder = (await api.noteOrder()).order;
	} catch {
		/* Falls back to alphabetical, which is the pre-existing behaviour. */
	}
}

/** Same optimistic-then-reconcile shape as saveFolderOrder. */
export async function saveNoteOrder(order: string[]) {
	vault.noteOrder = order;
	try {
		vault.noteOrder = (await api.saveNoteOrder(order)).order;
	} catch (cause) {
		await refreshNoteOrder();
		throw cause;
	}
}

export async function refreshTags() {
	vault.tags = await api.listTags();
}

/** Resolve a wikilink target. Names alone are ambiguous across folders, so
 * prefer an exact path, then a root-level note, then the shallowest match —
 * deterministic rather than "whichever came first in the list". */
export function findNoteByName(name: string): NoteListItem | undefined {
	const needle = name.trim().toLowerCase();
	if (!needle) return undefined;

	const withExt = needle.endsWith('.md') ? needle : `${needle}.md`;
	const exactPath = vault.notes.find((n) => n.path.toLowerCase() === withExt);
	if (exactPath) return exactPath;

	// Names are stored without the extension, so `[[Note.md]]` must still match.
	const bare = needle.replace(/\.md$/, '');
	const matches = vault.notes.filter((n) => n.name.toLowerCase() === bare);
	if (matches.length <= 1) return matches[0];

	// Shallowest path wins; ties broken alphabetically so it never flip-flops.
	return [...matches].sort((a, b) => {
		const depth = a.path.split('/').length - b.path.split('/').length;
		return depth !== 0 ? depth : a.path.localeCompare(b.path);
	})[0];
}

// --- OMNIA ------------------------------------------------------------------

export interface ChatMsg {
	id?: string;
	role: 'user' | 'assistant';
	text: string;
	status?: 'complete' | 'interrupted' | 'error' | 'streaming';
	createdAt?: string;
	contextNote?: string | null;
	trace?: { tool: string; input: string; ok?: boolean; error?: string }[];
	graphics?: GraphicSpec[];
	galleries?: MediaGallery[];
	actions?: OmniaAction[];
	confirmations?: OmniaConfirmation[];
	/** Vault attachment paths sent with the message, so they render on reload. */
	images?: string[];
}

export interface ChatState {
	messages: ChatMsg[];
	sessionId: string | null;
}

/** Full-page OMNIA chat (persists across navigation). */
export const overseerChat: ChatState = $state({ messages: [], sessionId: null });

/** How many OMNIA turns are running right now, and how many of those are media
 * work — powers the "OMNIA working…" / "OMNIA(M) working…" tab-bar indicator. */
export const omniaActivity = $state({ turns: 0, mediaTurns: 0 });

/** What the user is looking at right now, sent as context with each OMNIA
 * message so "rewrite this" / "what's on this screen?" resolve. `selection` is
 * the live editor selection (empty when nothing is selected); `view` is a label
 * for the current route. Open tabs are read from the tabs store at send time. */
export const omniaContext = $state({
	selection: '',
	view: '',
	// Exact selection offsets, so OMNIA's selection-editing tools act on the
	// precise range; `docLen` is a cheap staleness proxy the backend checks.
	from: 0,
	to: 0,
	head: 0,
	docLen: 0,
	path: ''
});

/** Tools that mean the running turn is media-related (drives the (M) badge). */
export const MEDIA_TOOLS = new Set([
	'lookup_media',
	'log_media',
	'update_media',
	'search_media_images',
	'set_media_art',
	'set_media_review',
	'media_details',
	'similar_media',
	'media_stats'
]);

export const omniaThreads = $state({
	items: [] as ThreadSummary[],
	loaded: false,
	loadingThread: false
});

let storedThreadId: string | null = null;
if (typeof localStorage !== 'undefined') {
	storedThreadId = localStorage.getItem('exscalia.omnia.activeThread');
	overseerChat.sessionId = storedThreadId;
}

export async function refreshThreads() {
	try {
		omniaThreads.items = (await api.listThreads()).threads;
		omniaThreads.loaded = true;
	} catch {
		// Keep whatever the rail is already showing. This used to empty the list,
		// which turned a dropped request into a conversation list that vanished —
		// and now that the preloader fills the list before /omnia is ever opened,
		// the page's own refresh on mount would have thrown that work away.
		if (!omniaThreads.loaded) omniaThreads.items = [];
	}
}

export async function openOmniaThread(id: string): Promise<OmniaThread> {
	omniaThreads.loadingThread = true;
	try {
		const thread = await api.getThread(id);
		overseerChat.sessionId = thread.id;
		overseerChat.messages = thread.messages.map((message) => ({
			id: message.id,
			role: message.role,
			text: message.text,
			status: message.status,
			createdAt: message.created_at,
			contextNote: message.context_note,
			trace: message.trace,
			graphics: message.graphics,
			galleries: message.galleries,
			actions: message.actions,
			confirmations: message.confirmations,
			images: message.images
		}));
		if (typeof localStorage !== 'undefined') {
			localStorage.setItem('exscalia.omnia.activeThread', id);
		}
		return thread;
	} finally {
		omniaThreads.loadingThread = false;
	}
}

export async function ensureOmniaThread(): Promise<string> {
	if (overseerChat.sessionId) {
		const active = overseerChat.sessionId;
		try {
			await openOmniaThread(active);
			return active;
		} catch (cause) {
			// A thread that is genuinely gone releases the session; anything else —
			// a dropped request, a server mid-restart — must not detach the user
			// from the conversation whose transcript is already on screen.
			if ((cause as { status?: number }).status !== 404) throw cause;
			overseerChat.sessionId = null;
		}
	}
	await refreshThreads();
	const first = omniaThreads.items[0];
	if (first) {
		await openOmniaThread(first.id);
		return first.id;
	}
	const created = await api.createThread();
	await openOmniaThread(created.id);
	await refreshThreads();
	return created.id;
}

/** Per-note conversations.
 *
 * The note drawer used to pass the same global `overseerChat`, so asking about
 * a note wrote into whichever thread happened to be active — nearly always
 * today's daily thread, which quietly corrupted the day's record. Each note now
 * owns its own chat state and its own backend thread.
 */
// The registry is a plain object; each chat inside it is its own $state.
//
// It used to be one big `$state({})`, which made `noteChatFor` — a lazy
// creator — mutate reactive state. The note page calls it while rendering the
// chat drawer, and mutating $state during render throws state_unsafe_mutation
// in runes mode: the panel's update aborted half-done, so opening OMNIA left
// the previous tab's contents on screen. Only the transcript needs to be
// reactive, never the set of cached paths.
const noteChats: Record<string, ChatState> = {};

function createChatState(): ChatState {
	const chat: ChatState = $state({ messages: [], sessionId: null });
	return chat;
}

const noteThreadKey = (path: string) => `exscalia.omnia.noteThread:${path}`;

// Every note visited kept a full transcript in memory for the life of the tab.
const MAX_CACHED_NOTE_CHATS = 20;
const noteChatOrder: string[] = [];

export function noteChatFor(path: string): ChatState {
	if (!noteChats[path]) {
		noteChats[path] = createChatState();
		noteChatOrder.push(path);
		while (noteChatOrder.length > MAX_CACHED_NOTE_CHATS) {
			const evicted = noteChatOrder.shift();
			// Only the in-memory copy goes; the thread itself and the pointer
			// to it survive, so reopening the note reloads the conversation.
			if (evicted && evicted !== path) delete noteChats[evicted];
		}
	}
	return noteChats[path];
}

/** Load a note's remembered conversation into its drawer.
 *
 * Opening the OMNIA tab showed an empty transcript even when the note had a
 * thread with a full history behind it — nothing loaded it until you sent
 * another message. Unlike `ensureNoteThread` this never creates a thread:
 * opening a drawer should not mint an empty conversation. */
export async function restoreNoteThread(path: string): Promise<void> {
	const chat = noteChatFor(path);
	if (chat.sessionId || chat.messages.length) return;
	let remembered: string | null = null;
	try {
		remembered = localStorage.getItem(noteThreadKey(path));
	} catch {
		return; // Private-mode storage; nothing was remembered anyway.
	}
	if (!remembered) return;
	try {
		await loadThreadInto(chat, remembered);
	} catch {
		// Deleted elsewhere — drop the dangling pointer.
		try {
			localStorage.removeItem(noteThreadKey(path));
		} catch {
			// Nothing to clean up.
		}
	}
}

/** Carry a note's conversation with it when the note is renamed or moved.
 *
 * Without this the pointer stayed on the old path, so the conversation was
 * orphaned and the next message silently created a *second* thread. Renaming
 * five times left five threads, four of them dead. */
export function moveNoteThread(oldPath: string, newPath: string): void {
	if (oldPath === newPath) return;
	try {
		const id = localStorage.getItem(noteThreadKey(oldPath));
		if (id) {
			localStorage.setItem(noteThreadKey(newPath), id);
			localStorage.removeItem(noteThreadKey(oldPath));
		}
	} catch {
		// Storage unavailable — the in-memory move below still applies.
	}
	if (noteChats[oldPath]) {
		noteChats[newPath] = noteChats[oldPath];
		delete noteChats[oldPath];
	}
}

/** Drop a deleted note's conversation pointer so it doesn't leak forever. */
export function forgetNoteThread(path: string): void {
	try {
		localStorage.removeItem(noteThreadKey(path));
	} catch {
		// Nothing to clean up.
	}
	delete noteChats[path];
}

/** Resolve (and remember) the thread backing a note's drawer. */
export async function ensureNoteThread(path: string): Promise<string> {
	const chat = noteChatFor(path);
	if (chat.sessionId) return chat.sessionId;

	let remembered: string | null = null;
	try {
		remembered = localStorage.getItem(noteThreadKey(path));
	} catch {
		// Private-mode storage: fall through and make a fresh thread.
	}
	if (remembered) {
		try {
			await loadThreadInto(chat, remembered);
			return remembered;
		} catch {
			// The thread was deleted — drop the stale pointer and recreate.
		}
	}
	const name = path.split('/').pop()?.replace(/\.md$/, '') ?? path;
	const created = await api.createThread(name);
	chat.sessionId = created.id;
	chat.messages = [];
	try {
		localStorage.setItem(noteThreadKey(path), created.id);
	} catch {
		// Not fatal — the thread just won't be remembered across reloads.
	}
	await refreshThreads();
	return created.id;
}

/** Load a thread's transcript into a specific chat state. */
export async function loadThreadInto(chat: ChatState, id: string): Promise<void> {
	const thread = await api.getThread(id);
	chat.sessionId = thread.id;
	chat.messages = thread.messages.map((message) => ({
		id: message.id,
		role: message.role,
		text: message.text,
		status: message.status,
		createdAt: message.created_at,
		contextNote: message.context_note,
		trace: message.trace,
		graphics: message.graphics,
		galleries: message.galleries,
		actions: message.actions,
		confirmations: message.confirmations,
		images: message.images
	}));
}

export async function createOmniaThread(): Promise<string> {
	const thread = await api.createThread();
	await openOmniaThread(thread.id);
	await refreshThreads();
	return thread.id;
}

export function activateOmniaThread(id: string) {
	overseerChat.sessionId = id;
	overseerChat.messages = [];
	if (typeof localStorage !== 'undefined') {
		localStorage.setItem('exscalia.omnia.activeThread', id);
	}
	void openOmniaThread(id).catch(() => {});
}

export const overseerStatus = $state({
	available: false,
	model: null as string | null,
	loaded: false
});

export async function refreshOverseerStatus() {
	try {
		const s = await api.overseerStatus();
		overseerStatus.available = s.available;
		overseerStatus.model = s.model;
	} catch {
		overseerStatus.available = false;
	} finally {
		overseerStatus.loaded = true;
	}
}
