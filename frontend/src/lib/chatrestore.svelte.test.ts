// restoreNoteThread runs inside effects — the note's OMNIA tab, its drawer and
// the media popover. It read the chat before its first await, so the calling
// effect subscribed to the transcript the reload then replaced: a thread whose
// last reply the server saved as interrupted was read again and again for as
// long as the surface stayed open, and so was every chat after a Stop.
import { flushSync } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ThreadMessage } from './api';
import { noteChatFor, restoreNoteThread, type ChatMsg } from './stores.svelte';

const stub = vi.hoisted(() => ({
	getThread: vi.fn(),
	createThread: vi.fn()
}));

vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, ...stub } };
});

const storage = new Map<string, string>();
vi.stubGlobal('localStorage', {
	getItem: (key: string) => storage.get(key) ?? null,
	setItem: (key: string, value: string) => void storage.set(key, value),
	removeItem: (key: string) => void storage.delete(key)
});

const saved = (
	id: string,
	role: ThreadMessage['role'],
	text: string,
	status: ThreadMessage['status']
): ThreadMessage => ({ id, role, text, created_at: '2026-09-23T09:00:00Z', status });

/** The server's copy: its last reply stopped short. A loop, if there is one,
 * ends once it reads as complete, so a failing run cannot hang the suite. */
function serveInterrupted(id: string) {
	let reads = 0;
	stub.getThread.mockImplementation(async () => {
		reads += 1;
		return {
			id,
			title: 'Plans',
			created_at: '2026-09-23T09:00:00Z',
			updated_at: '2026-09-23T09:01:00Z',
			messages: [
				saved('m1', 'user', 'Plan my week', 'complete'),
				saved('m2', 'assistant', 'Monday: gym.', reads > 20 ? 'complete' : 'interrupted')
			]
		};
	});
}

/** Every reload, and every effect it would re-run, has had its turn. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

let cleanup: (() => void) | undefined;

/** The call sites' shape: an effect keyed on the path it opens. */
function openSurface(path: string) {
	cleanup = $effect.root(() => {
		$effect(() => {
			void restoreNoteThread(path);
		});
	});
	flushSync();
}

describe('restoreNoteThread inside an effect', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		storage.clear();
	});

	afterEach(() => {
		cleanup?.();
		cleanup = undefined;
	});

	it('reads a thread whose last reply was interrupted once', async () => {
		serveInterrupted('thread-a');
		const chat = noteChatFor('A.md');
		chat.sessionId = 'thread-a';
		chat.messages = [
			{ id: 'm1', role: 'user', text: 'Plan my week', status: 'complete' },
			{ id: 'm2', role: 'assistant', text: 'Monday', status: 'interrupted' }
		];

		openSurface('A.md');
		await settle();
		expect(stub.getThread).toHaveBeenCalledTimes(1);
		expect(chat.messages.map((m) => m.text)).toEqual(['Plan my week', 'Monday: gym.']);
	});

	it('does not read the thread again when a turn in it is stopped', async () => {
		serveInterrupted('thread-b');
		const chat = noteChatFor('B.md');
		chat.sessionId = 'thread-b';
		chat.messages = [
			{ id: 'm1', role: 'user', text: 'Plan my week', status: 'complete' },
			{ id: 'm2', role: 'assistant', text: 'Monday: gym.', status: 'complete' }
		];
		openSurface('B.md');
		await settle();
		expect(stub.getThread).not.toHaveBeenCalled();

		// What a panel does from Send to Stop: local ids, then the stream ends.
		chat.streaming = true;
		const question: ChatMsg = {
			id: 'local-user-1',
			role: 'user',
			text: 'And Tuesday?',
			status: 'complete'
		};
		const reply: ChatMsg = {
			id: 'local-assistant-1',
			role: 'assistant',
			text: '',
			status: 'streaming'
		};
		chat.messages.push(question, reply);
		flushSync();
		const live = chat.messages[chat.messages.length - 1];
		live.status = 'interrupted';
		live.text = 'Response stopped.';
		chat.streaming = false;
		await settle();
		expect(stub.getThread).not.toHaveBeenCalled();
	});
});
