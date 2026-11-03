// When a transcript already in memory is trusted and when it is read again.
// A turn cut off by a closed surface or a failed stream left its bubble (and a
// Retry that re-runs the turn) on screen for good, because anything with
// messages counted as loaded. And a re-read must never land on a transcript a
// turn has started writing into since it was sent.
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ThreadMessage } from './api';
import type { ChatMsg, ChatState } from './stores.svelte';

const stub = vi.hoisted(() => ({
	getThread: vi.fn(),
	createThread: vi.fn()
}));

vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, ...stub } };
});

const PATH = 'Inbox/Plans.md';
const POINTER = `exscalia.omnia.noteThread:${PATH}`;

const storage = new Map<string, string>();
vi.stubGlobal('localStorage', {
	getItem: (key: string) => storage.get(key) ?? null,
	setItem: (key: string, value: string) => void storage.set(key, value),
	removeItem: (key: string) => void storage.delete(key)
});

/** A fresh store module per test, so no cached chat carries over. */
async function load() {
	vi.resetModules();
	return await import('./stores.svelte');
}

/** A promise the test decides the fate of, so a load can be caught mid-flight. */
function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((res) => (resolve = res));
	return { promise, resolve };
}

const question = (id: string): ChatMsg => ({
	id,
	role: 'user',
	text: 'Plan my week',
	status: 'complete'
});

const reply = (id: string, status: ChatMsg['status'], text = 'Monday: gym.'): ChatMsg => ({
	id,
	role: 'assistant',
	text,
	status
});

const saved = (id: string, role: ThreadMessage['role'], text: string): ThreadMessage => ({
	id,
	role,
	text,
	created_at: '2026-09-23T09:00:00Z',
	status: 'complete'
});

const THREAD = {
	id: 'thread-plans',
	title: 'Plans',
	created_at: '2026-09-23T09:00:00Z',
	updated_at: '2026-09-23T09:01:00Z',
	messages: [
		saved('m1', 'user', 'Plan my week'),
		saved('m2', 'assistant', 'Monday: gym. Tuesday: groceries.')
	]
};

describe('isChatDirty', () => {
	const chat = (messages: ChatMsg[], streaming?: boolean): ChatState => ({
		messages,
		sessionId: 'thread-plans',
		...(streaming === undefined ? {} : { streaming })
	});

	it('is clean with nothing loaded', async () => {
		const { isChatDirty } = await load();
		expect(isChatDirty(chat([]))).toBe(false);
	});

	it('is clean once the last reply is the server copy, finished', async () => {
		const { isChatDirty } = await load();
		expect(isChatDirty(chat([question('m1'), reply('m2', 'complete')]))).toBe(false);
	});

	it('is dirty after a turn cut off before the server settled it', async () => {
		const { isChatDirty } = await load();
		const cut = chat([
			question('local-user-1'),
			reply('local-assistant-1', 'interrupted', 'Reply continued on the server.')
		]);
		expect(isChatDirty(cut)).toBe(true);
		// A failed stream keeps its local ids whatever the status says.
		expect(isChatDirty(chat([question('local-user-1'), reply('local-assistant-1', 'error')]))).toBe(
			true
		);
	});

	it('is clean while a panel here streams the reply', async () => {
		const { isChatDirty } = await load();
		const live = chat([question('local-user-1'), reply('local-assistant-1', 'streaming', '')], true);
		expect(isChatDirty(live)).toBe(false);
	});

	it('is dirty when the reply is being written somewhere no panel here is reading', async () => {
		const { isChatDirty } = await load();
		expect(isChatDirty(chat([question('m1'), reply('m2', 'streaming', 'Monday')]))).toBe(true);
		expect(isChatDirty(chat([question('m1'), reply('m2', 'streaming', 'Monday')], false))).toBe(
			true
		);
	});
});

describe('hasUnsettledMessages', () => {
	const chat = (messages: ChatMsg[]): ChatState => ({ messages, sessionId: 'thread-plans' });

	it('is settled when every message is the server copy', async () => {
		const { hasUnsettledMessages } = await load();
		const current = chat([question('m1'), reply('m2', 'complete'), reply('m3', 'interrupted')]);
		expect(hasUnsettledMessages(current)).toBe(false);
	});

	it('finds an earlier reply still partial behind a finished turn', async () => {
		const { hasUnsettledMessages } = await load();
		// Reopened mid-reply, then a follow-up sent: the server queued it
		// behind the first, and `done` settled only the second.
		const behind = chat([
			question('m1'),
			reply('m2', 'streaming', 'Monday'),
			question('m3'),
			reply('m4', 'complete', 'Tuesday: groceries.')
		]);
		const [, , asked, answered] = behind.messages;
		expect(hasUnsettledMessages(behind, [asked, answered])).toBe(true);
	});

	it('finds an earlier turn cut off before the server settled it', async () => {
		const { hasUnsettledMessages } = await load();
		const cut = chat([
			question('local-user-1'),
			reply('local-assistant-1', 'interrupted', 'Response stopped.'),
			question('m3'),
			reply('m4', 'complete')
		]);
		const [, , asked, answered] = cut.messages;
		expect(hasUnsettledMessages(cut, [asked, answered])).toBe(true);
	});

	it('leaves out the turn being settled', async () => {
		const { hasUnsettledMessages } = await load();
		const live = chat([
			question('m1'),
			reply('m2', 'complete'),
			question('local-user-2'),
			reply('local-assistant-2', 'streaming', '')
		]);
		const [, , asked, answered] = live.messages;
		expect(hasUnsettledMessages(live, [asked, answered])).toBe(false);
		expect(hasUnsettledMessages(live)).toBe(true);
	});
});

describe('restoreNoteThread', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		storage.clear();
		storage.set(POINTER, THREAD.id);
	});

	it('reads a transcript cut off mid-reply again from the server', async () => {
		stub.getThread.mockResolvedValue(THREAD);
		const { noteChatFor, restoreNoteThread } = await load();
		const chat = noteChatFor(PATH);
		chat.sessionId = THREAD.id;
		chat.messages = [
			question('local-user-1'),
			reply('local-assistant-1', 'interrupted', 'Reply continued on the server.')
		];

		await restoreNoteThread(PATH);
		expect(stub.getThread).toHaveBeenCalledWith(THREAD.id);
		expect(chat.messages.map((m) => [m.id, m.status, m.text])).toEqual([
			['m1', 'complete', 'Plan my week'],
			['m2', 'complete', 'Monday: gym. Tuesday: groceries.']
		]);
		expect(stub.createThread).not.toHaveBeenCalled();
	});

	it('keeps a transcript that is current', async () => {
		const { noteChatFor, restoreNoteThread } = await load();
		const chat = noteChatFor(PATH);
		chat.sessionId = THREAD.id;
		chat.messages = [question('m1'), reply('m2', 'complete')];

		await restoreNoteThread(PATH);
		expect(stub.getThread).not.toHaveBeenCalled();
	});

	it('leaves a reply alone while another view is streaming it', async () => {
		const { noteChatFor, restoreNoteThread } = await load();
		const chat = noteChatFor(PATH);
		chat.sessionId = THREAD.id;
		chat.messages = [question('local-user-1'), reply('local-assistant-1', 'streaming', '')];
		chat.streaming = true;

		await restoreNoteThread(PATH);
		expect(stub.getThread).not.toHaveBeenCalled();
	});

	it('keeps the transcript and its pointer when the re-read fails', async () => {
		stub.getThread.mockRejectedValue(new TypeError('Load failed'));
		const { noteChatFor, restoreNoteThread } = await load();
		const chat = noteChatFor(PATH);
		chat.sessionId = THREAD.id;
		chat.messages = [question('local-user-1'), reply('local-assistant-1', 'error', 'Monday')];

		await restoreNoteThread(PATH);
		expect(chat.messages.map((m) => m.id)).toEqual(['local-user-1', 'local-assistant-1']);
		expect(storage.get(POINTER)).toBe(THREAD.id);
	});
});

describe('loadThreadInto', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('does not replace a transcript a turn began streaming into meanwhile', async () => {
		const gate = deferred<typeof THREAD>();
		stub.getThread.mockReturnValue(gate.promise);
		const { loadThreadInto, noteChatFor } = await load();
		const chat = noteChatFor(PATH);
		chat.sessionId = THREAD.id;
		chat.messages = [question('m1'), reply('m2', 'streaming', 'Monday')];

		const loading = loadThreadInto(chat, THREAD.id);
		chat.messages.push(question('local-user-2'), reply('local-assistant-2', 'streaming', ''));
		chat.streaming = true;
		const live = chat.messages[chat.messages.length - 1];
		gate.resolve(THREAD);
		await loading;

		expect(chat.messages[chat.messages.length - 1]).toBe(live);
		expect(chat.messages).toHaveLength(4);
	});

	it('does not pull a chat back to a thread it has left', async () => {
		const gate = deferred<typeof THREAD>();
		stub.getThread.mockReturnValue(gate.promise);
		const { loadThreadInto, noteChatFor } = await load();
		const chat = noteChatFor(PATH);
		chat.sessionId = THREAD.id;
		chat.messages = [question('m1'), reply('m2', 'streaming', 'Monday')];

		const loading = loadThreadInto(chat, THREAD.id);
		chat.sessionId = 'thread-other';
		chat.messages = [];
		gate.resolve(THREAD);
		await loading;

		expect(chat.sessionId).toBe('thread-other');
		expect(chat.messages).toEqual([]);
	});

	it('still settles the turn that is streaming when it asks for the server copy', async () => {
		stub.getThread.mockResolvedValue(THREAD);
		const { loadThreadInto, noteChatFor } = await load();
		const chat = noteChatFor(PATH);
		chat.sessionId = THREAD.id;
		chat.messages = [question('local-user-1'), reply('local-assistant-1', 'streaming', 'Mon')];
		chat.streaming = true;

		await loadThreadInto(chat, THREAD.id);
		expect(chat.messages.map((m) => m.id)).toEqual(['m1', 'm2']);
	});
});
