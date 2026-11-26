// The app-shell worker, run against an in-memory Cache Storage: what the
// install precaches, and which navigation responses may become the offline
// shell. A deploy's 502 or a JSON/icon tab must never replace index.html.
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$service-worker', () => ({
	build: [
		'/_app/immutable/entry/start.js',
		'/_app/immutable/assets/app.css',
		'/_app/immutable/assets/KaTeX_Main-Regular.a1.woff2',
		'/_app/immutable/assets/KaTeX_Main-Regular.b2.woff',
		'/_app/immutable/assets/KaTeX_Main-Regular.c3.ttf'
	],
	files: ['/icon-192.png', '/manifest.webmanifest'],
	version: 'test'
}));

const ORIGIN = 'https://exscalia.test';

type Key = string | { url: string };
const pathOf = (key: Key) =>
	typeof key === 'string' ? key : new URL(key.url, ORIGIN).pathname;

/** One cache is all the worker ever opens per version. */
const store = new Map<string, Response>();
const cache = {
	addAll: vi.fn(async (paths: string[]) => {
		const responses = await Promise.all(paths.map((p) => fetchMock(p)));
		if (responses.some((r) => !r.ok)) throw new TypeError('addAll: bad response');
		paths.forEach((p, i) => store.set(p, responses[i]));
	}),
	add: vi.fn(async (path: string) => {
		const response = await fetchMock(path);
		if (!response.ok) throw new TypeError('add: bad response');
		store.set(path, response);
	}),
	put: vi.fn(async (key: Key, response: Response) => {
		store.set(pathOf(key), response);
	}),
	match: async (key: Key) => store.get(pathOf(key))?.clone()
};

const fetchMock = vi.fn<(input: Key) => Promise<Response>>();
const handlers: Record<string, (event: unknown) => void> = {};
const skipWaiting = vi.fn(async () => {});

const html = (body: string) =>
	new Response(body, { headers: { 'content-type': 'text/html; charset=utf-8' } });

/** Dispatch a fetch event; resolves to what the worker answered plus its settled waitUntils. */
async function navigate(path: string, accept = 'text/html,application/xhtml+xml') {
	const waits: Promise<unknown>[] = [];
	let answer: Promise<Response> | undefined;
	handlers.fetch({
		request: {
			method: 'GET',
			mode: 'navigate',
			url: ORIGIN + path,
			headers: new Headers({ accept })
		},
		respondWith: (p: Promise<Response>) => (answer = p),
		waitUntil: (p: Promise<unknown>) => waits.push(p)
	});
	const response = await answer!;
	await Promise.all(waits);
	return response;
}

beforeAll(async () => {
	vi.stubGlobal('self', {
		addEventListener: (type: string, fn: (event: unknown) => void) => (handlers[type] = fn),
		registration: { active: null },
		skipWaiting,
		clients: { claim: async () => {}, matchAll: async () => [] }
	});
	vi.stubGlobal('location', { origin: ORIGIN });
	vi.stubGlobal('fetch', fetchMock);
	vi.stubGlobal('caches', {
		open: async () => cache,
		match: (key: Key) => cache.match(key),
		keys: async () => ['shell-test'],
		delete: async () => true
	});
	await import('./service-worker');
});

beforeEach(() => {
	store.clear();
	vi.clearAllMocks();
});

describe('install', () => {
	async function install() {
		let done: Promise<unknown> | undefined;
		handlers.install({ waitUntil: (p: Promise<unknown>) => (done = p) });
		await done;
	}

	it('precaches the shell without the unreachable KaTeX ttf/woff fallbacks', async () => {
		fetchMock.mockImplementation(async () => new Response('x'));
		await install();

		const atomic = cache.addAll.mock.calls[0][0];
		expect(atomic).toContain('/_app/immutable/entry/start.js');
		expect(atomic).toContain('/_app/immutable/assets/KaTeX_Main-Regular.a1.woff2');
		expect(atomic).toContain('/index.html');
		const fallbackFont = (p: string) => /\.(ttf|woff)$/.test(p);
		expect(atomic.filter(fallbackFont)).toEqual([]);
		expect(fetchMock.mock.calls.map(([p]) => pathOf(p)).filter(fallbackFont)).toEqual([]);
		expect(store.has('/icon-192.png')).toBe(true);
		expect(skipWaiting).toHaveBeenCalledOnce();
	});

	it('still activates when a static file fails to download', async () => {
		fetchMock.mockImplementation(async (p) =>
			pathOf(p) === '/manifest.webmanifest' ? new Response('', { status: 503 }) : new Response('x')
		);
		await install();

		expect(store.has('/index.html')).toBe(true);
		expect(store.has('/manifest.webmanifest')).toBe(false);
		expect(skipWaiting).toHaveBeenCalledOnce();
	});
});

describe('navigation', () => {
	const shellText = async () => (await caches.match('/index.html'))?.text();

	beforeEach(() => {
		store.set('/index.html', html('cached shell'));
	});

	it('serves the network page and refreshes the cached shell with it', async () => {
		fetchMock.mockResolvedValue(html('fresh shell'));
		const response = await navigate('/notes/a');

		expect(await response.text()).toBe('fresh shell');
		expect(await shellText()).toBe('fresh shell');
	});

	it('answers a 502 during a deploy with the cached shell and keeps it', async () => {
		fetchMock.mockResolvedValue(new Response('Bad Gateway', { status: 502 }));
		const response = await navigate('/notes/a');

		expect(await response.text()).toBe('cached shell');
		expect(cache.put).not.toHaveBeenCalled();
	});

	it('passes the 502 through when there is no cached shell yet', async () => {
		store.clear();
		fetchMock.mockResolvedValue(new Response('Bad Gateway', { status: 502 }));
		const response = await navigate('/notes/a');

		expect(response.status).toBe(502);
		expect(store.has('/index.html')).toBe(false);
	});

	it('never stores the update feed or an icon opened in a tab', async () => {
		fetchMock.mockResolvedValueOnce(
			new Response('{"version":"1"}', { headers: { 'content-type': 'application/json' } })
		);
		expect(await (await navigate('/updates/latest.json')).text()).toBe('{"version":"1"}');

		fetchMock.mockResolvedValueOnce(
			new Response('png', { headers: { 'content-type': 'image/png' } })
		);
		expect(await (await navigate('/icon-512.png')).text()).toBe('png');

		expect(cache.put).not.toHaveBeenCalled();
		expect(await shellText()).toBe('cached shell');
	});

	it('does not answer a failing update feed or icon with the shell', async () => {
		fetchMock.mockResolvedValue(new Response('Bad Gateway', { status: 502 }));

		expect((await navigate('/updates/latest.json')).status).toBe(502);
		expect((await navigate('/icon-192.png')).status).toBe(502);
	});

	it('does not answer a non-HTML navigation with the shell on a 5xx', async () => {
		fetchMock.mockResolvedValue(new Response('Bad Gateway', { status: 502 }));
		const response = await navigate('/notes/a', '*/*');

		expect(response.status).toBe(502);
	});

	it('never stores a redirected response', async () => {
		const redirected = html('login page');
		Object.defineProperty(redirected, 'redirected', { value: true });
		fetchMock.mockResolvedValue(redirected);
		await navigate('/notes/a');

		expect(cache.put).not.toHaveBeenCalled();
		expect(await shellText()).toBe('cached shell');
	});

	it('falls back to the cached shell when the network is gone', async () => {
		fetchMock.mockRejectedValue(new TypeError('offline'));
		const response = await navigate('/notes/a');

		expect(await response.text()).toBe('cached shell');
	});
});
