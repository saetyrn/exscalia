// The desktop live connection (SSE over fetch). A half-open socket after a
// wake from sleep never errors, so a stream that stays silent past the
// watchdog is torn down and reconnected at once; kick() reconnects without
// waiting out the backoff. live.test.ts covers the same transport from the
// wake side.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./runtime', () => ({ apiBase: () => '', authHeaders: () => ({}), isDesktop: true }));

import { connectLive, WATCHDOG_MS, type LiveConnection, type LiveState } from './live';

/** Captured before any test fakes timers: settling must never move the clock. */
const realSetTimeout = globalThis.setTimeout;

async function settle() {
	for (let i = 0; i < 5; i++) await new Promise((resolve) => realSetTimeout(resolve, 0));
}

/** GET /api/live. Each request gets a body the test writes into by hand, and
 * aborting the request errors that body, as a real fetch does. */
const server = {
	bodies: [] as ReadableStreamDefaultController<Uint8Array>[],
	signals: [] as AbortSignal[],
	/** How many upcoming requests fail as if the server were unreachable. */
	failing: 0
};

/** Write into the newest request's stream. */
function send(text: string) {
	server.bodies[server.bodies.length - 1].enqueue(new TextEncoder().encode(text));
}

const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
	const signal = init.signal!;
	server.signals.push(signal);
	if (server.failing > 0) {
		server.failing -= 1;
		throw new TypeError('Load failed');
	}
	let body!: ReadableStreamDefaultController<Uint8Array>;
	const stream = new ReadableStream<Uint8Array>({ start: (c) => void (body = c) });
	signal.addEventListener('abort', () => body.error(new DOMException('Aborted', 'AbortError')));
	server.bodies.push(body);
	return new Response(stream, { status: 200 });
});

let states: LiveState[] = [];
let connection: LiveConnection | null = null;

function connect() {
	connection = connectLive(() => {}, { onState: (state) => states.push(state) });
	return connection;
}

async function advance(ms: number) {
	await vi.advanceTimersByTimeAsync(ms);
	await settle();
}

beforeEach(() => {
	vi.useFakeTimers();
	vi.stubGlobal('fetch', fetchMock);
	fetchMock.mockClear();
	server.bodies = [];
	server.signals = [];
	server.failing = 0;
	states = [];
});

afterEach(() => {
	connection?.();
	connection = null;
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('idle watchdog', () => {
	it('keeps a stream that sends keepalives', async () => {
		connect();
		await settle();
		send('event: ready\ndata: {}\n\n');
		await settle();
		expect(states).toEqual(['connecting', 'live']);

		for (let i = 0; i < 5; i++) {
			await advance(20_000);
			send(': keepalive\n\n');
			await settle();
		}

		expect(server.signals[0].aborted).toBe(false);
		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it('aborts a stream that goes silent after ready, then reconnects', async () => {
		connect();
		await settle();
		send('event: ready\ndata: {}\n\n');
		await settle();

		await advance(WATCHDOG_MS - 1);
		expect(server.signals[0].aborted).toBe(false);
		await advance(1);

		// Torn down and reopened at once: a stall is not a refusal to back off from.
		expect(server.signals[0].aborted).toBe(true);
		expect(states.slice(-2)).toEqual(['offline', 'connecting']);
		expect(fetchMock).toHaveBeenCalledTimes(2);
		await advance(1_000);
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('gives up on a request that never answers', async () => {
		fetchMock.mockImplementationOnce(
			(_url, init) =>
				new Promise((_resolve, reject) => {
					server.signals.push(init.signal!);
					init.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
				})
		);
		connect();

		await advance(WATCHDOG_MS);

		expect(server.signals[0].aborted).toBe(true);
		await advance(1_000);
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});
});

describe('kick', () => {
	it('reconnects at once during the backoff, and the old timer never fires', async () => {
		server.failing = 3;
		const live = connect();
		await settle();
		await advance(1_000);
		await advance(2_000);
		// Third failure: the next attempt waits 4s.
		expect(fetchMock).toHaveBeenCalledTimes(3);
		expect(states.at(-1)).toBe('offline');

		live.kick!();
		await settle();

		expect(fetchMock).toHaveBeenCalledTimes(4);
		send('event: ready\ndata: {}\n\n');
		await settle();
		expect(states.at(-1)).toBe('live');
		await advance(10_000);
		expect(fetchMock).toHaveBeenCalledTimes(4);
	});

	it('replaces an open stream without a second reconnect from the old one', async () => {
		const live = connect();
		await settle();
		send('event: ready\ndata: {}\n\n');
		await settle();

		live.kick!();
		await settle();

		expect(server.signals[0].aborted).toBe(true);
		expect(fetchMock).toHaveBeenCalledTimes(2);
		// The aborted attempt reports nothing; only the new one's state shows.
		expect(states).toEqual(['connecting', 'live', 'connecting']);
		await advance(5_000);
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('resets the backoff', async () => {
		server.failing = 4;
		const live = connect();
		await settle();
		await advance(1_000);
		await advance(2_000);
		expect(fetchMock).toHaveBeenCalledTimes(3);

		live.kick!();
		await settle();
		// The kicked attempt failed too; its retry waits 1s, not 8s.
		expect(fetchMock).toHaveBeenCalledTimes(4);
		await advance(1_000);
		expect(fetchMock).toHaveBeenCalledTimes(5);
	});

	it('does nothing once the connection is closed', async () => {
		const live = connect();
		await settle();
		live();
		connection = null;

		live.kick!();
		await advance(60_000);

		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(vi.getTimerCount()).toBe(0);
	});
});
