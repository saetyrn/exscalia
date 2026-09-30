// saveNote's keepalive opt-in: the note page asks for it so an autosave PUT
// outlives a page that is hidden and then killed. It must reach fetch, and
// stay off unless asked (browsers reject keepalive bodies over 64 KiB).
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';

function captureFetch() {
	const fetch = vi.fn(
		async (_url: string, _init?: RequestInit) =>
			new Response(
				JSON.stringify({ path: 'a.md', content: 'x', meta: {}, revision: 'r2', status: 'saved' }),
				{ status: 200 }
			)
	);
	vi.stubGlobal('fetch', fetch);
	return fetch;
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('saveNote keepalive', () => {
	it('passes keepalive through to fetch when asked', async () => {
		const fetch = captureFetch();
		await api.saveNote('a.md', 'x', 'r1', 'reject', { keepalive: true });
		const init = fetch.mock.calls[0][1];
		expect(init?.method).toBe('PUT');
		expect(init?.keepalive).toBe(true);
		expect(JSON.parse(String(init?.body))).toMatchObject({
			content: 'x',
			base_revision: 'r1',
			on_conflict: 'reject'
		});
	});

	it('leaves it off when the caller does not ask', async () => {
		const fetch = captureFetch();
		await api.saveNote('a.md', 'x', 'r1', 'reject');
		expect(fetch.mock.calls[0][1]?.keepalive).toBeFalsy();
		await api.saveNote('a.md', 'x', 'r1', 'reject', { keepalive: false });
		expect(fetch.mock.calls[1][1]?.keepalive).toBe(false);
	});
});
