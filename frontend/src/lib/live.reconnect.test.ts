// Reconnection for the live stream: backoff, its reset on `ready`, and the
// two ways a revoked session stops the retry loop instead of hammering it.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const runtime = vi.hoisted(() => ({ desktop: true }));

vi.mock('./runtime', () => ({
	get isDesktop() {
		return runtime.desktop;
	},
	apiBase: () => '',
	authHeaders: () => ({})
}));

import { connectLive } from './live';

function sse(text: string): Response {
	return new Response(
		new ReadableStream<Uint8Array>({
			start(controller) {
				controller.enqueue(new TextEncoder().encode(text));
				controller.close();
			}
		})
	);
}

/** Advance to just before `delay` and check nothing fired, then to it. */
async function expectCallAfter(spy: { mock: { calls: unknown[] } }, delay: number) {
	const before = spy.mock.calls.length;
	await vi.advanceTimersByTimeAsync(delay - 1);
	expect(spy.mock.calls.length).toBe(before);
	await vi.advanceTimersByTimeAsync(1);
	expect(spy.mock.calls.length).toBe(before + 1);
}

beforeEach(() => {
	vi.useFakeTimers();
});

afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
	runtime.desktop = true;
});

describe('connectLive over fetch (desktop)', () => {
	it('doubles the retry delay up to 30s', async () => {
		const fetch = vi.fn().mockRejectedValue(new TypeError('Load failed'));
		vi.stubGlobal('fetch', fetch);
		const states: string[] = [];
		const close = connectLive(() => {}, { onState: (state) => states.push(state) });

		expect(fetch).toHaveBeenCalledTimes(1);
		for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
			await expectCallAfter(fetch, delay);
		}
		expect(states.slice(0, 3)).toEqual(['connecting', 'offline', 'connecting']);
		close();
	});

	it('resets the delay once the server says ready', async () => {
		let calls = 0;
		const fetch = vi.fn(async () => {
			calls += 1;
			if (calls === 3) {
				return sse('event: ready\ndata: {}\n\nevent: note\ndata: {"path":"a.md","revision":"r1"}\n\n');
			}
			throw new TypeError('Load failed');
		});
		vi.stubGlobal('fetch', fetch);
		const events: unknown[] = [];
		const states: string[] = [];
		const close = connectLive((event) => events.push(event), {
			onState: (state) => states.push(state)
		});

		await expectCallAfter(fetch, 1000);
		await expectCallAfter(fetch, 2000); // this one reaches ready, then the server closes
		expect(states).toContain('live');
		expect(events).toEqual([{ path: 'a.md', revision: 'r1', type: 'note' }]);
		await expectCallAfter(fetch, 1000);
		await expectCallAfter(fetch, 2000);
		close();
	});

	it('stops on a 401 and reports the expired session', async () => {
		const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 401 }));
		vi.stubGlobal('fetch', fetch);
		const onAuthExpired = vi.fn();
		const states: string[] = [];
		connectLive(() => {}, { onAuthExpired, onState: (state) => states.push(state) });

		await vi.advanceTimersByTimeAsync(0);
		expect(onAuthExpired).toHaveBeenCalledTimes(1);
		expect(states.at(-1)).toBe('offline');
		expect(vi.getTimerCount()).toBe(0);
		await vi.advanceTimersByTimeAsync(120_000);
		expect(fetch).toHaveBeenCalledTimes(1);
	});

	it('aborts an open stream when closed and never retries', async () => {
		let signal: AbortSignal | undefined;
		const fetch = vi.fn(
			(_url: string, init: RequestInit) =>
				new Promise<Response>((_resolve, reject) => {
					signal = init.signal ?? undefined;
					signal?.addEventListener('abort', () =>
						reject(new DOMException('The operation was aborted.', 'AbortError'))
					);
				})
		);
		vi.stubGlobal('fetch', fetch);
		const close = connectLive(() => {});

		close();
		expect(signal?.aborted).toBe(true);
		await vi.advanceTimersByTimeAsync(120_000);
		expect(fetch).toHaveBeenCalledTimes(1);
	});

	it('cancels a pending retry when closed', async () => {
		const fetch = vi.fn().mockRejectedValue(new TypeError('Load failed'));
		vi.stubGlobal('fetch', fetch);
		const close = connectLive(() => {});

		await vi.advanceTimersByTimeAsync(0);
		expect(vi.getTimerCount()).toBe(1);
		close();
		expect(vi.getTimerCount()).toBe(0);
		await vi.advanceTimersByTimeAsync(120_000);
		expect(fetch).toHaveBeenCalledTimes(1);
	});
});

describe('connectLive over EventSource (web)', () => {
	const log: string[] = [];

	class FakeEventSource {
		static instances: FakeEventSource[] = [];
		onerror: (() => void) | null = null;
		closed = false;
		private listeners = new Map<string, ((event: MessageEvent) => void)[]>();

		constructor(readonly url: string) {
			FakeEventSource.instances.push(this);
			log.push(`open ${url}`);
		}

		addEventListener(type: string, listener: (event: MessageEvent) => void) {
			this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
		}

		emit(type: string, data = '{}') {
			for (const listener of this.listeners.get(type) ?? []) {
				listener({ data } as MessageEvent);
			}
		}

		close() {
			this.closed = true;
		}
	}

	const latest = () => FakeEventSource.instances[FakeEventSource.instances.length - 1];

	function stubStatus(status: { auth_required: boolean; authenticated: boolean }) {
		const fetch = vi.fn(async (url: string) => {
			log.push(`probe ${url}`);
			return Response.json(status);
		});
		vi.stubGlobal('fetch', fetch);
		return fetch;
	}

	beforeEach(() => {
		runtime.desktop = false;
		log.length = 0;
		FakeEventSource.instances = [];
		vi.stubGlobal('EventSource', FakeEventSource);
	});

	it('asks the auth status route before each retry', async () => {
		stubStatus({ auth_required: true, authenticated: true });
		const close = connectLive(() => {});

		latest().onerror?.();
		expect(latest().closed).toBe(true);
		await vi.advanceTimersByTimeAsync(999);
		expect(log).toEqual(['open /api/live']);
		await vi.advanceTimersByTimeAsync(1);
		expect(log).toEqual(['open /api/live', 'probe /api/auth/status', 'open /api/live']);
		close();
	});

	it('stops retrying once the server says the session is gone', async () => {
		stubStatus({ auth_required: true, authenticated: false });
		const onAuthExpired = vi.fn();
		connectLive(() => {}, { onAuthExpired });

		latest().onerror?.();
		await vi.advanceTimersByTimeAsync(1000);
		expect(onAuthExpired).toHaveBeenCalledTimes(1);
		expect(FakeEventSource.instances).toHaveLength(1);
		expect(vi.getTimerCount()).toBe(0);
	});

	it('keeps retrying through an outage, backing off until ready', async () => {
		const fetch = vi.fn().mockRejectedValue(new TypeError('Load failed'));
		vi.stubGlobal('fetch', fetch);
		const events: unknown[] = [];
		const close = connectLive((event) => events.push(event));

		for (const delay of [1000, 2000, 4000]) {
			latest().onerror?.();
			await vi.advanceTimersByTimeAsync(delay - 1);
			expect(fetch).toHaveBeenCalledTimes(FakeEventSource.instances.length - 1);
			await vi.advanceTimersByTimeAsync(1);
		}
		expect(FakeEventSource.instances).toHaveLength(4);

		latest().emit('ready');
		latest().emit('note', '{"path":"a.md","revision":"r1"}');
		expect(events).toEqual([{ path: 'a.md', revision: 'r1', type: 'note' }]);
		latest().onerror?.();
		await vi.advanceTimersByTimeAsync(1000);
		expect(FakeEventSource.instances).toHaveLength(5);
		close();
		expect(latest().closed).toBe(true);
	});
});
