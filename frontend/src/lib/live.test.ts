// The live stream's reconnect loop. What matters is when a new connection
// opens: after the backoff, never after the session is gone, at once when the
// stream goes silent, and at once when the network or the page comes back.
// The web transport (EventSource) carries most of it; the desktop one (fetch)
// shares the watchdog and the wake, with its own way of abandoning a stream.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

class FakeEventSource {
	static instances: FakeEventSource[] = [];
	closed = false;
	onerror: (() => void) | null = null;
	private listeners = new Map<string, ((event: { data: string }) => void)[]>();

	constructor(readonly url: string) {
		FakeEventSource.instances.push(this);
	}

	addEventListener(type: string, listener: (event: { data: string }) => void) {
		this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
	}

	close() {
		this.closed = true;
	}

	/** A named frame from the server. A closed source hears nothing more. */
	fire(type: string, data: unknown = {}) {
		if (this.closed) return;
		for (const listener of this.listeners.get(type) ?? []) listener({ data: JSON.stringify(data) });
	}

	fail() {
		if (!this.closed) this.onerror?.();
	}
}

const windowTarget = new EventTarget();
const documentTarget = Object.assign(new EventTarget(), {
	visibilityState: 'visible' as DocumentVisibilityState
});
vi.stubGlobal('window', windowTarget);
vi.stubGlobal('document', documentTarget);
vi.stubGlobal('EventSource', FakeEventSource);

let authStatus = { auth_required: true, authenticated: true };
const fetchStub = vi.fn(async () => ({ json: async () => authStatus }));
vi.stubGlobal('fetch', fetchStub);

const { connectLive, WATCHDOG_MS } = await import('./live');

const sources = FakeEventSource.instances;
const latest = () => sources[sources.length - 1];

describe('connectLive over EventSource', () => {
	let stop: () => void;
	let states: string[];
	let expired: Mock<() => void>;
	let events: unknown[];

	beforeEach(() => {
		vi.useFakeTimers();
		sources.splice(0);
		fetchStub.mockClear();
		authStatus = { auth_required: true, authenticated: true };
		states = [];
		events = [];
		expired = vi.fn<() => void>();
		stop = connectLive((event) => events.push(event), {
			onState: (state) => states.push(state),
			onAuthExpired: expired
		});
	});

	afterEach(() => {
		stop();
		vi.clearAllTimers();
		vi.useRealTimers();
	});

	it('opens one stream and reports live on ready', () => {
		expect(sources).toHaveLength(1);
		expect(latest().url).toBe('/api/live');
		latest().fire('ready');
		expect(states).toEqual(['connecting', 'live']);
		latest().fire('note', { path: 'a.md', revision: 'r1', origin: null });
		expect(events).toEqual([{ path: 'a.md', revision: 'r1', origin: null, type: 'note' }]);
	});

	it('delivers a move with both paths', () => {
		latest().fire('ready');
		const move = { path: 'a/One.md', new_path: 'b/One.md', revision: 'r2', origin: null };
		latest().fire('note_moved', move);
		expect(events).toEqual([{ ...move, type: 'note_moved' }]);
	});

	it('backs off on repeated failures and resets once ready', async () => {
		latest().fail();
		expect(states.at(-1)).toBe('offline');
		await vi.advanceTimersByTimeAsync(999);
		expect(sources).toHaveLength(1);
		await vi.advanceTimersByTimeAsync(1);
		expect(sources).toHaveLength(2);

		latest().fail();
		await vi.advanceTimersByTimeAsync(1999);
		expect(sources).toHaveLength(2);
		await vi.advanceTimersByTimeAsync(1);
		expect(sources).toHaveLength(3);

		latest().fire('ready');
		latest().fail();
		await vi.advanceTimersByTimeAsync(1000);
		expect(sources).toHaveLength(4);
	});

	it('stops for good once the server says the session is gone', async () => {
		authStatus = { auth_required: true, authenticated: false };
		latest().fail();
		await vi.advanceTimersByTimeAsync(1000);
		expect(fetchStub).toHaveBeenCalledWith('/api/auth/status');
		expect(expired).toHaveBeenCalledTimes(1);
		expect(sources).toHaveLength(1);

		// Neither a wake nor time brings the dead session back.
		windowTarget.dispatchEvent(new Event('online'));
		await vi.advanceTimersByTimeAsync(60_000);
		expect(sources).toHaveLength(1);
		expect(expired).toHaveBeenCalledTimes(1);
	});

	it('keeps a stream that is pinging', async () => {
		latest().fire('ready');
		for (let i = 0; i < 6; i++) {
			await vi.advanceTimersByTimeAsync(20_000);
			latest().fire('ping');
		}
		expect(sources).toHaveLength(1);
		expect(sources[0].closed).toBe(false);
	});

	it('replaces a stream that went silent, without waiting out a backoff', async () => {
		latest().fire('ready');
		await vi.advanceTimersByTimeAsync(20_000);
		latest().fire('ping');
		await vi.advanceTimersByTimeAsync(WATCHDOG_MS - 1);
		expect(sources).toHaveLength(1);
		await vi.advanceTimersByTimeAsync(1);
		expect(sources).toHaveLength(2);
		expect(sources[0].closed).toBe(true);
		expect(states).toEqual(['connecting', 'live', 'offline', 'connecting']);
	});

	it('also watches a connection that never reaches ready', async () => {
		await vi.advanceTimersByTimeAsync(WATCHDOG_MS);
		expect(sources).toHaveLength(2);
		expect(sources[0].closed).toBe(true);
	});

	it('reconnects at once when the network returns while offline', async () => {
		// Four failures deep, the next attempt is 8 s away.
		for (let i = 0; i < 3; i++) {
			latest().fail();
			await vi.runOnlyPendingTimersAsync();
		}
		latest().fail();
		const before = sources.length;

		windowTarget.dispatchEvent(new Event('online'));
		expect(sources).toHaveLength(before + 1);
		expect(states.at(-1)).toBe('connecting');

		// The abandoned backoff timer does not open a second stream...
		await vi.advanceTimersByTimeAsync(8_000);
		expect(sources).toHaveLength(before + 1);
		// ...and the backoff starts over.
		latest().fail();
		await vi.advanceTimersByTimeAsync(1000);
		expect(sources).toHaveLength(before + 2);
	});

	it('leaves a healthy stream alone on wake', () => {
		latest().fire('ready');
		windowTarget.dispatchEvent(new Event('online'));
		documentTarget.dispatchEvent(new Event('visibilitychange'));
		expect(sources).toHaveLength(1);
	});

	it('replaces a stream that went quiet while the machine slept', () => {
		latest().fire('ready');
		// Sleep: the wall clock moves, timers do not fire.
		vi.setSystemTime(Date.now() + WATCHDOG_MS + 5_000);
		documentTarget.dispatchEvent(new Event('visibilitychange'));
		expect(sources).toHaveLength(2);
		expect(sources[0].closed).toBe(true);
	});

	it('an auth check that outlives its attempt does not open a second stream', async () => {
		let answer!: (value: { json: () => Promise<typeof authStatus> }) => void;
		fetchStub.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)));
		latest().fail();
		await vi.advanceTimersByTimeAsync(1000);
		expect(fetchStub).toHaveBeenCalledTimes(1);

		documentTarget.dispatchEvent(new Event('visibilitychange'));
		expect(sources).toHaveLength(2);

		answer({ json: async () => authStatus });
		await vi.advanceTimersByTimeAsync(0);
		expect(sources).toHaveLength(2);
	});

	it('stop closes the stream and ignores later wakes', async () => {
		stop();
		expect(sources[0].closed).toBe(true);
		windowTarget.dispatchEvent(new Event('online'));
		await vi.advanceTimersByTimeAsync(WATCHDOG_MS * 2);
		expect(sources).toHaveLength(1);
	});
});

/** A streaming /api/live response the test writes frames into. Aborting the
 * request errors the body, as a real fetch does. */
function liveResponse(signal: AbortSignal) {
	let controller!: ReadableStreamDefaultController<Uint8Array>;
	const body = new ReadableStream<Uint8Array>({ start: (c) => (controller = c) });
	const stream = {
		aborted: false,
		send: (event: string, data: unknown = {}) =>
			controller.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)),
		end: () => controller.close()
	};
	signal.addEventListener('abort', () => {
		stream.aborted = true;
		controller.error(new DOMException('Aborted', 'AbortError'));
	});
	return { stream, response: new Response(body, { status: 200 }) };
}

describe('connectLive over fetch (desktop)', () => {
	let connect: typeof connectLive;
	let streams: ReturnType<typeof liveResponse>['stream'][];
	let status: number;
	let stop: () => void;
	let states: string[];
	let expired: Mock<() => void>;

	beforeAll(async () => {
		// isDesktop is decided when runtime.ts loads; load a desktop copy.
		vi.resetModules();
		Object.assign(windowTarget, { __TAURI_INTERNALS__: {} });
		connect = (await import('./live')).connectLive;
	});

	beforeEach(() => {
		vi.useFakeTimers();
		streams = [];
		status = 200;
		fetchStub.mockReset();
		fetchStub.mockImplementation((async (_url: string, init: RequestInit) => {
			if (status !== 200) return new Response(null, { status });
			const { stream, response } = liveResponse(init.signal!);
			streams.push(stream);
			return response;
		}) as never);
		states = [];
		expired = vi.fn<() => void>();
		stop = connect(() => {}, { onState: (s) => states.push(s), onAuthExpired: expired });
	});

	afterEach(() => {
		stop();
		vi.clearAllTimers();
		vi.useRealTimers();
	});

	it('reads the stream and keeps it while pings arrive', async () => {
		await vi.advanceTimersByTimeAsync(0);
		expect(streams).toHaveLength(1);
		streams[0].send('ready');
		await vi.advanceTimersByTimeAsync(0);
		expect(states).toEqual(['connecting', 'live']);
		for (let i = 0; i < 6; i++) {
			await vi.advanceTimersByTimeAsync(20_000);
			streams[0].send('ping');
		}
		await vi.advanceTimersByTimeAsync(0);
		expect(streams).toHaveLength(1);
		expect(streams[0].aborted).toBe(false);
	});

	it('abandons a silent stream and reconnects at once, once', async () => {
		await vi.advanceTimersByTimeAsync(0);
		streams[0].send('ready');
		await vi.advanceTimersByTimeAsync(WATCHDOG_MS);
		expect(streams[0].aborted).toBe(true);
		expect(streams).toHaveLength(2);
		expect(states).toEqual(['connecting', 'live', 'offline', 'connecting']);
		// The abandoned read's rejection does not schedule a second retry.
		await vi.advanceTimersByTimeAsync(5_000);
		expect(streams).toHaveLength(2);
	});

	it('reconnects at once on wake while waiting out the backoff', async () => {
		await vi.advanceTimersByTimeAsync(0);
		streams[0].end();
		await vi.advanceTimersByTimeAsync(0);
		expect(states.at(-1)).toBe('offline');
		documentTarget.dispatchEvent(new Event('visibilitychange'));
		await vi.advanceTimersByTimeAsync(0);
		expect(streams).toHaveLength(2);
		await vi.advanceTimersByTimeAsync(1_000);
		expect(streams).toHaveLength(2);
	});

	it('passes moves through with the note events, and nothing unknown', async () => {
		stop();
		const received: unknown[] = [];
		stop = connect((event) => received.push(event), { onState: (s) => states.push(s) });
		await vi.advanceTimersByTimeAsync(0);
		const stream = streams[streams.length - 1];
		const move = { path: 'a/One.md', new_path: 'b/One.md', revision: 'r2', origin: null };
		stream.send('ready');
		stream.send('note_moved', move);
		stream.send('mystery', { path: 'x.md' });
		await vi.advanceTimersByTimeAsync(0);
		expect(received).toEqual([{ ...move, type: 'note_moved' }]);
	});

	it('stops for good on a 401', async () => {
		stop();
		status = 401;
		stop = connect(() => {}, { onState: (s) => states.push(s), onAuthExpired: expired });
		await vi.advanceTimersByTimeAsync(0);
		expect(expired).toHaveBeenCalledTimes(1);
		windowTarget.dispatchEvent(new Event('online'));
		await vi.advanceTimersByTimeAsync(WATCHDOG_MS * 2);
		expect(fetchStub).toHaveBeenCalledTimes(2);
		expect(expired).toHaveBeenCalledTimes(1);
	});
});
