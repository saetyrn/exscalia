// Settling a finished reply from the `done` event. The panel no longer reloads
// the thread at the end of a turn, so the saved copy is applied over the one it
// streamed into — and a card confirmed or a change undone while the reply was
// still being written must survive a saved copy that predates it.
import { describe, expect, it } from 'vitest';

import type { OmniaAction, OmniaConfirmation, ThreadMessage } from './api';
import { settleReply, type ChatMsg } from './stores.svelte';

const card = (token: string, status?: OmniaConfirmation['status']): OmniaConfirmation => ({
	token,
	tool: 'delete_note',
	summary: `Delete ${token}`,
	created_at: '2026-09-23T10:00:00Z',
	expires_at: '2026-09-23T10:10:00Z',
	...(status ? { status } : {})
});

const action = (id: string, extra: Partial<OmniaAction> = {}): OmniaAction => ({
	id,
	tool: 'delete_note',
	summary: `Deleted ${id}`,
	paths: [],
	refresh: ['notes'],
	commit: null,
	created_at: '2026-09-23T10:00:05Z',
	undoable: true,
	...extra
});

const saved = (over: Partial<ThreadMessage> = {}): ThreadMessage => ({
	id: 'srv-assistant',
	role: 'assistant',
	text: 'Done.',
	created_at: '2026-09-23T10:00:00Z',
	status: 'complete',
	trace: [{ tool: 'delete_note', input: 'A.md' }],
	actions: [],
	confirmations: [card('t1')],
	usage: { input: 900, output: 40 },
	...over
});

const streamed = (over: Partial<ChatMsg> = {}): ChatMsg => ({
	id: 'local-assistant-1',
	role: 'assistant',
	text: 'Done',
	status: 'streaming',
	running: null,
	trace: [],
	actions: [],
	confirmations: [card('t1')],
	...over
});

describe('settleReply', () => {
	it('takes the saved id, status, text and usage', () => {
		const next = settleReply(streamed(), saved());
		expect(next.id).toBe('srv-assistant');
		expect(next.status).toBe('complete');
		expect(next.text).toBe('Done.');
		expect(next.usage).toEqual({ input: 900, output: 40 });
	});

	it('keeps a card confirmed mid-turn when the saved copy predates it', () => {
		const local = streamed({
			confirmations: [card('t1', 'approved')],
			actions: [action('a1')]
		});
		const next = settleReply(local, saved());
		expect(next.confirmations?.map((c) => [c.token, c.status])).toEqual([['t1', 'approved']]);
		expect(next.actions?.map((a) => a.id)).toEqual(['a1']);
	});

	it('does not duplicate an action the saved copy already has', () => {
		const local = streamed({
			confirmations: [card('t1', 'approved')],
			actions: [action('a1')]
		});
		const next = settleReply(
			local,
			saved({ confirmations: [card('t1', 'approved')], actions: [action('a1')] })
		);
		expect(next.actions?.map((a) => a.id)).toEqual(['a1']);
	});

	it('lets a status the server settled stand', () => {
		const next = settleReply(streamed(), saved({ confirmations: [card('t1', 'cancelled')] }));
		expect(next.confirmations?.[0].status).toBe('cancelled');
	});

	it('keeps a change undone mid-turn undone', () => {
		const local = streamed({ actions: [action('a1', { undoable: false, undone: true })] });
		const next = settleReply(local, saved({ actions: [action('a1')] }));
		expect(next.actions).toHaveLength(1);
		expect(next.actions?.[0]).toMatchObject({ id: 'a1', undoable: false, undone: true });
	});
});
