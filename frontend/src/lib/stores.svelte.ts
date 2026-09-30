import { untrack } from 'svelte';

import {
	api,
	ApiError,
	type GraphicSpec,
	type MediaGallery,
	type NoteListItem,
	type OmniaAction,
	type OmniaConfirmation,
	type OmniaThread,
	type OmniaUsage,
	type TagCount,
	type ThreadMessage,
	type ThreadSummary
} from './api';
import { moveDraft, removeAllDrafts } from './drafts';
import { closeTabsUnder, retargetTabs } from './tabs.svelte';
import { resolveWikilink } from './wikilinks';

/** Connection status behind the sidebar's sync dot. Desktop: the sync
 * engine's state. Web: mirrors the live stream (the layout forwards its
 * connecting/live/offline), so a dead connection shows there too; `replaying`
 * and `pending` are desktop-only. */
export const syncState = $state({
	mode: 'live' as 'offline' | 'connecting' | 'replaying' | 'live',
	/** Local changes waiting to reach the server. */
	pending: 0,
	progressDone: 0,
	progressTotal: 0,
	/** The latest sync failure that was not a lost connection (a server error,
	 * a refused note, a cache fault); cleared by a clean replay. */
	lastError: '' as string
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

function persistRecentNotes() {
	try {
		localStorage.setItem('exscalia.recentNotes', JSON.stringify(recentNotes.paths));
	} catch {
		/* private mode — the jump-back shortcuts just won't survive a reload */
	}
}

export function rememberNote(path: string) {
	if (!path || recentNotes.paths[0] === path) return;
	recentNotes.paths = [path, ...recentNotes.paths.filter((p) => p !== path)].slice(0, RECENT_LIMIT);
	persistRecentNotes();
}

/** A renamed note keeps its place in the list instead of dropping off it. */
export function renameRecent(oldPath: string, newPath: string) {
	if (!recentNotes.paths.includes(oldPath)) return;
	recentNotes.paths = [...new Set(recentNotes.paths.map((p) => (p === oldPath ? newPath : p)))];
	persistRecentNotes();
}

export function forgetRecent(path: string) {
	if (!recentNotes.paths.includes(path)) return;
	recentNotes.paths = recentNotes.paths.filter((p) => p !== path);
	persistRecentNotes();
}

/** Everything this browser keys by a note's path, carried along when notes
 * move: the unsaved draft, the OMNIA thread pointer and its cached chat, the
 * tabs on it and its place among the recents. One helper for every mover —
 * the sidebar, the note page, and the live stream for moves made on another
 * device — because each used to carry a different subset, and a folder
 * rename carried none of it. */
export async function afterPathsMoved(pairs: [string, string][]): Promise<void> {
	const moves = pairs.filter(([oldPath, newPath]) => oldPath && newPath && oldPath !== newPath);
	// Everything but the drafts is synchronous and goes first: an open note
	// following a remote move rebinds its drawer to the new path, and must
	// find the conversation already there.
	for (const [oldPath, newPath] of moves) {
		moveNoteThread(oldPath, newPath);
		retargetTabs(oldPath, newPath);
		renameRecent(oldPath, newPath);
	}
	for (const [oldPath, newPath] of moves) await moveDraft(oldPath, newPath);
}

/** The same bookkeeping for notes sent to the Trash, so their drafts and
 * thread pointers do not leak forever. The active tab is the caller's to
 * navigate away; only background tabs close. */
export async function afterPathsRemoved(paths: string[]): Promise<void> {
	const gone = paths.filter(Boolean);
	for (const path of gone) {
		forgetNoteThread(path);
		closeTabsUnder(path);
		forgetRecent(path);
	}
	for (const path of gone) await removeAllDrafts(path);
}

export async function refreshPendingCount() {
	try {
		vault.pendingCount = (await api.pendingCount()).count;
	} catch {
		/* backend not ready */
	}
}

/** The note list and the saved arrangement, applied together. A rename
 * rewrites the arrangement server-side (sidebar_order.py), so a list fetched
 * without it would sink the renamed note, or a renamed folder, to the bottom. */
export async function refreshNotes() {
	const folderToken = folderOrderSeq;
	const noteToken = noteOrderSeq;
	const [notes, folders, folderOrder, noteOrder] = await Promise.all([
		api.listNotes(),
		api.listFolders(),
		readOrder(api.folderOrder),
		readOrder(api.noteOrder)
	]);
	// Most refetches (a live echo, a burst that moved nothing) return exactly
	// what is here; reassigning anyway re-derives the whole sidebar tree and
	// the palette. Same echo guard as the board's load().
	if (JSON.stringify(notes) !== JSON.stringify(vault.notes)) vault.notes = notes;
	if (JSON.stringify(folders) !== JSON.stringify(vault.folders)) vault.folders = folders;
	// A save that began meanwhile owns the arrangement (see the seq tokens).
	if (folderOrder && folderToken === folderOrderSeq) vault.folderOrder = folderOrder;
	if (noteOrder && noteToken === noteOrderSeq) vault.noteOrder = noteOrder;
	vault.loaded = true;
}

async function readOrder(load: () => Promise<{ order: string[] }>): Promise<string[] | null> {
	try {
		return (await load()).order;
	} catch {
		/* Falls back to alphabetical, which is the pre-existing behaviour. */
		return null;
	}
}

/** Bumped by every save, so only the latest one's outcome reaches the
 * screen — an earlier reply (or re-read) landing late would snap back a later
 * drag. */
let folderOrderSeq = 0;
let noteOrderSeq = 0;

/** Read the saved folder arrangement on its own — the rollback path when
 * saving a new one fails. */
export async function refreshFolderOrder() {
	const token = folderOrderSeq;
	const order = await readOrder(api.folderOrder);
	if (order && token === folderOrderSeq) vault.folderOrder = order;
}

/** Persist the arrangement. The full list is sent so entries for folders that
 * have since been renamed or deleted are dropped server-side. A failure rolls
 * back locally: the re-read usually fails for the same reason the save did
 * (offline, 401, restart), so it can not be what undoes the apply. */
export async function saveFolderOrder(order: string[]) {
	const previous = vault.folderOrder;
	const token = ++folderOrderSeq;
	vault.folderOrder = order;
	try {
		const saved = (await api.saveFolderOrder(order)).order;
		if (token === folderOrderSeq) vault.folderOrder = saved;
	} catch (cause) {
		if (token === folderOrderSeq) {
			vault.folderOrder = previous;
			void refreshFolderOrder();
		}
		throw cause;
	}
}

export async function refreshNoteOrder() {
	const token = noteOrderSeq;
	const order = await readOrder(api.noteOrder);
	if (order && token === noteOrderSeq) vault.noteOrder = order;
}

/** Same optimistic-then-reconcile shape as saveFolderOrder. */
export async function saveNoteOrder(order: string[]) {
	const previous = vault.noteOrder;
	const token = ++noteOrderSeq;
	vault.noteOrder = order;
	try {
		const saved = (await api.saveNoteOrder(order)).order;
		if (token === noteOrderSeq) vault.noteOrder = saved;
	} catch (cause) {
		if (token === noteOrderSeq) {
			vault.noteOrder = previous;
			void refreshNoteOrder();
		}
		throw cause;
	}
}

export async function refreshTags() {
	vault.tags = await api.listTags();
}

/** The note list keyed the way findNoteByName looks notes up, built once per
 * list. A chat reply resolves each of its wikilinks on every render, and
 * scanning (and folding) the whole vault per link made that render cost grow
 * with the vault. refreshNotes replaces the array rather than editing it, so
 * the array itself says when this is stale. Keys are folded like the server's
 * (wikilinks.ts: NFC, lowercase); candidates keep their spelling. */
let noteIndex: {
	notes: NoteListItem[];
	byPath: Map<string, NoteListItem[]>;
	byName: Map<string, NoteListItem[]>;
} | null = null;

const foldKey = (s: string) => s.normalize('NFC').toLowerCase();

function indexNotes(notes: NoteListItem[]) {
	if (noteIndex?.notes === notes) return noteIndex;
	// Untracked: the caller's effect depends on the list, not on every note in it.
	noteIndex = untrack(() => {
		const byPath = new Map<string, NoteListItem[]>();
		const byName = new Map<string, NoteListItem[]>();
		for (const note of notes) {
			const path = foldKey(note.path);
			const paths = byPath.get(path);
			if (paths) paths.push(note);
			else byPath.set(path, [note]);
			const name = foldKey(note.name);
			const named = byName.get(name);
			if (named) named.push(note);
			else byName.set(name, [note]);
		}
		return { notes, byPath, byName };
	});
	return noteIndex;
}

/** Resolve a wikilink target: exact path, then the shallowest note of that
 * name, an exact-case spelling first (rules in wikilinks.ts, shared with the
 * server's backlinks, so what a click opens is what they count). The index
 * only narrows the list to the notes a target can name. */
export function findNoteByName(name: string): NoteListItem | undefined {
	const typed = name.trim().normalize('NFC');
	if (!typed) return undefined;
	const { byPath, byName } = indexNotes(vault.notes);
	const typedExt = /\.md$/i.test(typed) ? typed : `${typed}.md`;
	const paths = byPath.get(typedExt.toLowerCase());
	if (paths?.length) return resolveWikilink(typed, paths);
	const names = byName.get(typed.replace(/\.md$/i, '').toLowerCase());
	return names?.length ? resolveWikilink(typed, names) : undefined;
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
	/** The tool running right now, from the live-only `tool_start` event. Never
	 * persisted — a reloaded thread only ever carries the finished `trace`. */
	running?: { tool: string; summary: string } | null;
	graphics?: GraphicSpec[];
	galleries?: MediaGallery[];
	actions?: OmniaAction[];
	confirmations?: OmniaConfirmation[];
	/** Vault attachment paths sent with the message, so they render on reload. */
	images?: string[];
	/** What the turn cost, for the trace tooltip. Assistant messages only. */
	usage?: OmniaUsage;
}

/** ThreadMessage → ChatMsg.
 *
 * There were two copies of this mapping, and a field added to one of them
 * vanished from the other's surface the moment its thread reloaded. One
 * function now, called by both loaders.
 */
export function toChatMsg(message: ThreadMessage): ChatMsg {
	return {
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
		images: message.images,
		usage: message.usage
	};
}

/** A finished reply as the server saved it, applied over the copy the panel
 * streamed into.
 *
 * Confirmations and actions merge rather than replace: a card confirmed (or a
 * change undone) while the reply was still being written settles here first,
 * and the saved copy can predate it — replacing put the card back to pending
 * and dropped the Undo card of the change it had made. */
export function settleReply(local: ChatMsg, saved: ThreadMessage): ChatMsg {
	const next = toChatMsg(saved);
	const open = (c: OmniaConfirmation) => (c.status ?? 'pending') === 'pending';
	const settled = new Map(
		(local.confirmations ?? []).filter((c) => !open(c)).map((c) => [c.token, c.status])
	);
	const savedCards = new Set((next.confirmations ?? []).map((c) => c.token));
	next.confirmations = [
		...(next.confirmations ?? []).map((c) =>
			open(c) && settled.has(c.token) ? { ...c, status: settled.get(c.token) } : c
		),
		...(local.confirmations ?? []).filter((c) => !savedCards.has(c.token))
	];
	const undone = new Set((local.actions ?? []).filter((a) => a.undone).map((a) => a.id));
	const savedActions = new Set((next.actions ?? []).map((a) => a.id));
	next.actions = [
		...(next.actions ?? []).map((a) =>
			!a.undone && undone.has(a.id) ? { ...a, undoable: false, undone: true } : a
		),
		...(local.actions ?? []).filter((a) => !savedActions.has(a.id))
	];
	return next;
}

export interface ChatState {
	messages: ChatMsg[];
	sessionId: string | null;
	/** What was in the composer, and anything still queued behind a reply,
	 * when the last panel on this chat closed. The next one starts from it. */
	draft?: string;
	/** A panel is streaming a turn into these messages right now. On the chat,
	 * not the panel, so a second view of the same transcript (the note drawer
	 * over the note's OMNIA tab) knows its latest reply is live, not stale. */
	streaming?: boolean;
}

/** Whether a transcript may be behind the server's copy of its thread.
 *
 * Its latest message never got the server's final word: a turn cut off by a
 * closed surface or a failed stream (`done` settles every finished turn onto
 * server ids, so only those keep `local-` ones), or a reply still being
 * written that no panel here is streaming — another device's, or the server
 * finishing one whose reader went away. Reopening such a transcript used to
 * trust it because it had messages, and kept the stale bubble for good. */
export function isChatDirty(chat: ChatState): boolean {
	if (chat.streaming) return false;
	const last = chat.messages[chat.messages.length - 1];
	if (!last) return false;
	return (
		Boolean(last.id?.startsWith('local-')) ||
		last.status === 'interrupted' ||
		last.status === 'streaming'
	);
}

/** Whether any message but the turn a panel is settling (`except`) is short
 * of the server's final word: still being written, or left with the local ids
 * of a turn that was cut off. `done` settles only its own turn, so an earlier
 * reply that finished on the server meanwhile stayed partial — with no Copy
 * or Retry — until the page was reloaded. */
export function hasUnsettledMessages(chat: ChatState, except: ChatMsg[] = []): boolean {
	return chat.messages.some(
		(m) =>
			!except.includes(m) && (m.status === 'streaming' || Boolean(m.id?.startsWith('local-')))
	);
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

/** The stamp a conversation row carries on the mobile list, in the shape
 * Messages uses: today is a time, the day before is named, the rest of the
 * week is a weekday, and anything older falls back to a date. Pure so the
 * boundaries can be tested against fixed clocks — the caller passes `now`.
 *
 * Days are counted between local midnights rather than by elapsed hours:
 * 11pm and 1am are a day apart to a reader even though they are two hours
 * apart to a clock. */
export function threadTimeLabel(iso: string | null | undefined, now = new Date()): string {
	if (!iso) return '';
	const when = new Date(iso);
	if (Number.isNaN(when.getTime())) return '';
	const midnight = (value: Date) =>
		new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
	const days = Math.round((midnight(now) - midnight(when)) / 86_400_000);
	// Ahead of the clock (skew between a device and the server) still reads as
	// today: "in 3 days" is not a thing a conversation list says.
	if (days <= 0) return when.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
	if (days === 1) return 'Yesterday';
	if (days < 7) return when.toLocaleDateString(undefined, { weekday: 'long' });
	return when.toLocaleDateString(undefined, { year: '2-digit', month: 'numeric', day: 'numeric' });
}

let storedThreadId: string | null = null;
if (typeof localStorage !== 'undefined') {
	try {
		storedThreadId = localStorage.getItem('exscalia.omnia.activeThread');
	} catch {
		// Storage blocked: start without a remembered thread rather than
		// failing this module, and every page with it.
	}
	overseerChat.sessionId = storedThreadId;
}

function rememberActiveThread(id: string) {
	try {
		localStorage.setItem('exscalia.omnia.activeThread', id);
	} catch {
		// Not remembered across reloads; the open thread still works.
	}
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
		overseerChat.messages = thread.messages.map(toChatMsg);
		rememberActiveThread(id);
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

/** Every cached chat holds exactly one slot in noteChatOrder. A move or a
 * forget that left the old slot behind made the cap count it, so a path
 * re-created later sat there twice and was evicted early. */
function dropChatSlot(path: string) {
	const at = noteChatOrder.indexOf(path);
	if (at >= 0) noteChatOrder.splice(at, 1);
}

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
 * opening a drawer should not mint an empty conversation.
 *
 * Untracked: the note's OMNIA tab, its drawer and the media popover call this
 * from effects, which subscribed to the chat state it reads before its first
 * await — the transcript it then replaces among it. A reply the server saved
 * as interrupted stays dirty, so every reload re-ran the effect and read the
 * thread again, without end. The caller's effect keys on what it opens; a
 * reply still being written is ChatPanel's follow() to poll. */
export function restoreNoteThread(path: string): Promise<void> {
	return untrack(() => restoreNow(path));
}

async function restoreNow(path: string): Promise<void> {
	const chat = noteChatFor(path);
	// Another view of this transcript is writing a reply into it.
	if (chat.streaming) return;
	if (chat.sessionId || chat.messages.length) {
		// Loaded, and current unless its last turn ended short of the server's
		// copy (see isChatDirty). That one is read again; a failure leaves what
		// is on screen, and the pointer, for the next open to retry.
		if (!isChatDirty(chat)) return;
		if (chat.sessionId) {
			await loadThreadInto(chat, chat.sessionId).catch(() => {});
			return;
		}
	}
	const id = await noteThreadId(path);
	if (!id) return;
	try {
		await loadThreadInto(chat, id);
	} catch (err) {
		// Deleted elsewhere — drop the dangling pointer. Any other failure
		// (offline, signed out, a restart's 502) keeps it for the next open.
		if (isGone(err)) rememberNoteThread(path, null);
	}
}

/** The server's answer that a thread does not exist, as opposed to a request
 * that failed on the way (0), a session that expired (401) or a server
 * restarting (5xx). */
function isGone(err: unknown): boolean {
	return err instanceof ApiError && err.status === 404;
}

function rememberNoteThread(path: string, id: string | null): void {
	try {
		if (id) localStorage.setItem(noteThreadKey(path), id);
		else localStorage.removeItem(noteThreadKey(path));
	} catch {
		// Private-mode storage: the server's binding still finds it.
	}
}

/** The thread behind a note's drawer. The server binds it to the note and
 * carries it through every move, including ones this browser never saw (made
 * on another device, or by OMNIA while this one slept); the pointer here is
 * a cache of that binding, and the only record of a thread made before
 * bindings existed, which is bound on first use. */
async function noteThreadId(path: string): Promise<string | null> {
	let remembered: string | null = null;
	try {
		remembered = localStorage.getItem(noteThreadKey(path));
	} catch {
		// Private-mode storage: only the server can say.
	}
	// Every note path ends in .md; the media hub's key keeps a local thread.
	if (!path.endsWith('.md')) return remembered;
	// Only the server's answer that there is no thread (`id: null`, or the
	// bind's 404 for a thread that is gone) drops the pointer. Anything else,
	// a 401 or a 5xx as much as no connection, leaves the cache as all there
	// is: for a thread made before bindings it is the only record of its note.
	let bound: string | null;
	try {
		bound = (await api.noteThread(path)).id;
	} catch {
		return remembered;
	}
	if (!bound && remembered) {
		try {
			bound = (await api.bindNoteThread(path, remembered)).id;
		} catch (err) {
			if (!isGone(err)) return remembered;
			bound = null;
		}
	}
	if (bound !== remembered) rememberNoteThread(path, bound);
	return bound;
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
		dropChatSlot(newPath);
		const at = noteChatOrder.indexOf(oldPath);
		if (at >= 0) noteChatOrder[at] = newPath;
	}
}

/** Drop a deleted note's conversation pointer so it doesn't leak forever.
 * The server keeps its binding, so a note restored from the Trash finds the
 * conversation again. */
export function forgetNoteThread(path: string): void {
	try {
		localStorage.removeItem(noteThreadKey(path));
	} catch {
		// Nothing to clean up.
	}
	delete noteChats[path];
	dropChatSlot(path);
}

/** Resolve (and remember) the thread backing a note's drawer. `title` names a
 * conversation whose key is not a note path — see mediaHubChat below. */
export async function ensureNoteThread(path: string, title?: string): Promise<string> {
	const chat = noteChatFor(path);
	if (chat.sessionId) return chat.sessionId;

	const known = await noteThreadId(path);
	if (known) {
		try {
			await loadThreadInto(chat, known);
			return known;
		} catch (err) {
			// The thread was deleted — recreate, which replaces the pointer.
			// Any other failure is not a reason to start a second thread.
			if (!isGone(err)) throw err;
		}
	}
	const name = title ?? (path.split('/').pop()?.replace(/\.md$/, '') ?? path);
	// Bound to the note, so the server may answer with the thread another
	// device made for it in the meantime.
	const created = await api.createThread(name, path.endsWith('.md') ? path : undefined);
	chat.sessionId = created.id;
	chat.messages = created.messages.map(toChatMsg);
	rememberNoteThread(path, created.id);
	await refreshThreads();
	return created.id;
}

/** The media hub's own conversation.
 *
 * The hub's popover passed the global `overseerChat` with no thread of its
 * own, so "what should I watch tonight?" landed in whichever thread happened
 * to be active — nearly always the day's daily thread, the same corruption
 * the per-note chats were built to stop. The hub is a place like a note is, so
 * it goes through the same registry under a key no note path can collide with
 * (every note path ends in .md).
 */
const MEDIA_HUB_KEY = 'media:hub';

export function mediaHubChat(): ChatState {
	return noteChatFor(MEDIA_HUB_KEY);
}

export function ensureMediaHubThread(): Promise<string> {
	return ensureNoteThread(MEDIA_HUB_KEY, 'Media');
}

export function restoreMediaHubThread(): Promise<void> {
	return restoreNoteThread(MEDIA_HUB_KEY);
}

/** Load a thread's transcript into a specific chat state. */
export async function loadThreadInto(chat: ChatState, id: string): Promise<void> {
	const from = chat.sessionId;
	const streaming = chat.streaming;
	const thread = await api.getThread(id);
	// The chat moved on while this was out: to another thread, or into a turn
	// of its own, whose reply is being written into the very array this would
	// replace (and which settles itself from `done`).
	if (chat.sessionId !== from || (chat.streaming && !streaming)) return;
	chat.sessionId = thread.id;
	chat.messages = thread.messages.map(toChatMsg);
}

export async function createOmniaThread(): Promise<string> {
	const thread = await api.createThread();
	await openOmniaThread(thread.id);
	await refreshThreads();
	return thread.id;
}

/** A question typed where there is no thread to ask it in — the command
 * palette's free-text row — waiting for the OMNIA page to pick it up.
 *
 * `seq` is what the page watches: the text is cleared as it is taken, and a
 * reactive read of the text itself would then re-run the page's setup with an
 * empty hand-off. Only `seq` moves forward, and only when something is handed
 * over, so the palette reaches an /omnia that is already open. */
export const omniaHandoff = $state({ text: '', seq: 0 });

export function handOffToOmnia(text: string) {
	omniaHandoff.text = text;
	omniaHandoff.seq += 1;
}

/** Read once — a reload of /omnia must not ask the same question again. */
export function takeOmniaHandoff(): string {
	const text = omniaHandoff.text;
	omniaHandoff.text = '';
	return text;
}

export function activateOmniaThread(id: string) {
	overseerChat.sessionId = id;
	overseerChat.messages = [];
	rememberActiveThread(id);
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
