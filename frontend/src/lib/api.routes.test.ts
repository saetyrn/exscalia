// The unified-search query string. Handwriting hits are opt-in on the server
// (installed desktops that predate Paper never ask for them), so the flag has
// to survive serialization as `true`, and unset filters must stay off the URL.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';

function capture(): string[] {
	const urls: string[] = [];
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string) => {
			urls.push(url);
			return new Response(
				JSON.stringify({ query: '', semantic_available: false, results: [] }),
				{ status: 200 }
			);
		})
	);
	return urls;
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('api.unifiedSearch', () => {
	it('sends include_paper as true and drops unset filters', async () => {
		const urls = capture();
		await api.unifiedSearch('ärger', {
			folder: undefined,
			result_type: 'all',
			include_paper: true
		});
		const params = new URL(urls[0], 'http://x').searchParams;
		expect(params.get('q')).toBe('ärger');
		expect(params.get('include_paper')).toBe('true');
		expect(params.get('result_type')).toBe('all');
		expect(params.has('folder')).toBe(false);
	});

	it('leaves the flag off when it is not set', async () => {
		const urls = capture();
		await api.unifiedSearch('budget', { include_paper: false });
		expect(new URL(urls[0], 'http://x').searchParams.has('include_paper')).toBe(false);
	});
});

function captureRequests(): { url: string; init?: RequestInit }[] {
	const calls: { url: string; init?: RequestInit }[] = [];
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string, init?: RequestInit) => {
			calls.push({ url, init });
			return new Response('{}', { status: 200 });
		})
	);
	return calls;
}

// `DELETE /api/notes/folders/<path>` also matched every note under a top-level
// folder named `folders`, so the folder now rides in the body.
describe('api.deleteFolder', () => {
	it('posts the folder in the body', async () => {
		const calls = captureRequests();
		await api.deleteFolder('folders/sub dir');
		expect(calls[0].url).toMatch(/\/api\/notes\/folders\/delete$/);
		expect(calls[0].init?.method).toBe('POST');
		expect(JSON.parse(String(calls[0].init?.body))).toEqual({ path: 'folders/sub dir' });
	});
});

describe('api.addLink', () => {
	it('sends the base revision and this client id', async () => {
		const calls = captureRequests();
		await api.addLink('a/Hub.md', 'b/Spoke.md', 'rev-1');
		const body = JSON.parse(String(calls[0].init?.body));
		expect(body.target).toBe('b/Spoke.md');
		expect(body.base_revision).toBe('rev-1');
		expect(typeof body.client_id).toBe('string');
		expect(body.client_id.length).toBeGreaterThan(0);
	});
});
