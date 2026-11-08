// The request layer's failure paths: what a failed response reads as, who
// hears about a dead session, and when a request that never answers gives up.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, apiFetch } from './api';
import { CLIENT_ID } from './live';

function respond(body: unknown, status: number) {
	return vi.fn(async () =>
		body === null ? new Response(null, { status }) : new Response(JSON.stringify(body), { status })
	);
}

/** A fetch that only ever settles by being aborted, like a real one on a
 * connection that has gone quiet. */
function hangingFetch() {
	return vi.fn(
		(_url: string, init?: RequestInit) =>
			new Promise<Response>((_, reject) => {
				const signal = init?.signal;
				signal?.addEventListener('abort', () => reject(signal.reason), { once: true });
			})
	);
}

async function failure(promise: Promise<unknown>): Promise<ApiError> {
	const err = await promise.then(
		() => null,
		(e: unknown) => e
	);
	expect(err).toBeInstanceOf(ApiError);
	return err as ApiError;
}

let dispatchEvent: ReturnType<typeof vi.fn>;

beforeEach(() => {
	dispatchEvent = vi.fn();
	vi.stubGlobal('window', { dispatchEvent });
});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('error details', () => {
	it('flattens a FastAPI validation list into readable text', async () => {
		vi.stubGlobal('fetch', respond({ detail: [{ loc: ['body', 'n'], msg: 'bad' }] }, 422));
		const err = await failure(api.listNotes());
		expect(err.status).toBe(422);
		expect(err.message).toBe('n: bad');
	});

	it('joins several validation errors', async () => {
		vi.stubGlobal(
			'fetch',
			respond(
				{
					detail: [
						{ loc: ['body', 'n'], msg: 'too big' },
						{ loc: ['body', 'tags', 2], msg: 'too long' },
						{ msg: 'bare' }
					]
				},
				422
			)
		);
		expect((await failure(api.listNotes())).message).toBe(
			'n: too big; tags: too long; bare'
		);
	});

	it('names the status when the body says nothing (HTTP/2 has no statusText)', async () => {
		vi.stubGlobal('fetch', respond(null, 502));
		const err = await failure(api.listNotes());
		expect(err.status).toBe(502);
		expect(err.message).toBe('Request failed (502)');
	});

	it('passes a string detail through', async () => {
		vi.stubGlobal('fetch', respond({ detail: 'Note not found' }, 404));
		expect((await failure(api.listNotes())).message).toBe('Note not found');
	});
});

describe('a dead session', () => {
	it('announces a 401 so the layout can ask for the password', async () => {
		vi.stubGlobal('fetch', respond({ detail: 'Not authenticated' }, 401));
		const err = await failure(api.listNotes());
		expect(err.status).toBe(401);
		expect(dispatchEvent).toHaveBeenCalledTimes(1);
		expect((dispatchEvent.mock.calls[0][0] as Event).type).toBe('exscalia:auth-expired');
	});

	it('leaves a wrong password to the login form', async () => {
		vi.stubGlobal('fetch', respond({ detail: 'Wrong username or password' }, 401));
		const err = await failure(api.login('alice', 'nope'));
		expect(err.message).toBe('Wrong username or password');
		expect(dispatchEvent).not.toHaveBeenCalled();
	});

	it('does not announce other failures', async () => {
		vi.stubGlobal('fetch', respond({ detail: 'nope' }, 403));
		await failure(api.listNotes());
		expect(dispatchEvent).not.toHaveBeenCalled();
	});
});

describe('deadline', () => {
	it('gives up on a request that never answers', async () => {
		vi.stubGlobal('fetch', hangingFetch());
		const err = await failure(apiFetch('/api/notes', { timeoutMs: 20 }));
		expect(err.status).toBe(0);
		expect(err.message).toBe('Server unreachable');
	});

	it('reads a dropped connection the same way', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn(async () => {
				throw new TypeError('Failed to fetch');
			})
		);
		const err = await failure(api.listNotes());
		expect(err.status).toBe(0);
		expect(err.message).toBe('Server unreachable');
	});

	it("keeps the caller's own abort an AbortError", async () => {
		vi.stubGlobal('fetch', hangingFetch());
		const stop = new AbortController();
		const pending = apiFetch('/api/omnia', { signal: stop.signal, timeoutMs: 0 });
		stop.abort();
		await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
	});

	it('still times out, and still honours Stop, without AbortSignal.any', async () => {
		vi.stubGlobal('fetch', hangingFetch());
		const any = AbortSignal.any;
		Object.defineProperty(AbortSignal, 'any', { value: undefined, configurable: true });
		try {
			const quiet = new AbortController();
			const err = await failure(apiFetch('/api/notes', { signal: quiet.signal, timeoutMs: 20 }));
			expect(err.status).toBe(0);

			const stop = new AbortController();
			const pending = apiFetch('/api/notes', { signal: stop.signal, timeoutMs: 1000 });
			stop.abort();
			await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
		} finally {
			Object.defineProperty(AbortSignal, 'any', { value: any, configurable: true });
		}
	});
});

describe('client id', () => {
	// A delete has no body to carry client_id; without the header the note
	// page reads its own trash as "deleted on another device".
	it('names this tab on every request, deletes included', async () => {
		const fetch = respond({ ok: true }, 200);
		vi.stubGlobal('fetch', fetch);
		await api.deleteNote('a/Keep.md');
		await api.deleteFolder('b');
		await api.listNotes();
		for (const [, init] of fetch.mock.calls as unknown as [string, RequestInit][])
			expect((init.headers as Record<string, string>)['x-client-id']).toBe(CLIENT_ID);
	});

	it("keeps the caller's own headers", async () => {
		const fetch = respond({ ok: true }, 200);
		vi.stubGlobal('fetch', fetch);
		await api.renameFolder('a', 'b');
		const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
		expect(init.headers).toMatchObject({
			'content-type': 'application/json',
			'x-client-id': CLIENT_ID
		});
	});
});
