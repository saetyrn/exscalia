// The recipes client calls: one per route of the HTTP API, vault paths in the
// URL segment by segment, the multipart import, the bodyless 204 delete, and
// the desktop's offline wrappers leaving every one of them on the network.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '$lib/api';
import { CLIENT_ID } from '$lib/live';
import { emptyRecipe } from './types';

type Call = { url: string; init: RequestInit };

function serve(status = 200, body: unknown = {}): Call[] {
	const calls: Call[] = [];
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string, init: RequestInit) => {
			calls.push({ url, init });
			return status === 204
				? new Response(null, { status })
				: new Response(JSON.stringify(body), { status });
		})
	);
	return calls;
}

const json = (call: Call) => JSON.parse(call.init.body as string);

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('recipes client', () => {
	it('lists with only the filters that are set', async () => {
		const calls = serve(200, { recipes: [], tags: [] });
		await api.recipesList();
		await api.recipesList('lemon pasta', 'quick');
		expect(calls[0].url).toBe('/api/recipes');
		const params = new URL(calls[1].url, 'http://x').searchParams;
		expect(params.get('q')).toBe('lemon pasta');
		expect(params.get('tag')).toBe('quick');
	});

	it('addresses one recipe by its vault path', async () => {
		const calls = serve();
		const path = 'recipes/crème brûlée.md';
		await api.recipesGet(path);
		await api.recipesStartArt(path);
		await api.recipesArtStatus(path);
		expect(calls.map((c) => [c.init.method ?? 'GET', c.url])).toEqual([
			['GET', '/api/recipes/item/recipes/cr%C3%A8me%20br%C3%BBl%C3%A9e.md'],
			['POST', '/api/recipes/art/recipes/cr%C3%A8me%20br%C3%BBl%C3%A9e.md'],
			['GET', '/api/recipes/art/recipes/cr%C3%A8me%20br%C3%BBl%C3%A9e.md']
		]);
	});

	it('creates and updates with this tab as the origin', async () => {
		const calls = serve();
		const recipe = { ...emptyRecipe(), title: 'Carbonara' };
		await api.recipesCreate(recipe);
		await api.recipesUpdate('recipes/carbonara.md', recipe, 'r1');
		expect(calls[0].url).toBe('/api/recipes');
		expect(json(calls[0])).toMatchObject({ title: 'Carbonara', client_id: CLIENT_ID });
		expect(calls[1].init.method).toBe('PUT');
		expect(calls[1].url).toBe('/api/recipes/item/recipes/carbonara.md');
		expect(json(calls[1])).toEqual({ recipe, base_revision: 'r1', client_id: CLIENT_ID });
	});

	it('writes the menu order and reads drafts from text or a link', async () => {
		const calls = serve();
		await api.recipesOrder(['recipes/a.md', 'recipes/b.md']);
		await api.recipesImport({ url: 'https://example.com/pie' });
		await api.recipesStatus();
		expect([calls[0].init.method, calls[0].url, json(calls[0])]).toEqual([
			'PUT',
			'/api/recipes/order',
			{ order: ['recipes/a.md', 'recipes/b.md'] }
		]);
		expect([calls[1].init.method, calls[1].url, json(calls[1])]).toEqual([
			'POST',
			'/api/recipes/import',
			{ url: 'https://example.com/pie' }
		]);
		expect(calls[2].url).toBe('/api/recipes/status');
	});

	it('sends photos and PDFs as multipart, leaving the boundary to fetch', async () => {
		const calls = serve();
		const photo = new File(['jpeg'], 'page1.jpg', { type: 'image/jpeg' });
		const pdf = new File(['pdf'], 'card.pdf', { type: 'application/pdf' });
		await api.recipesImportFiles([photo, pdf], 'serves 4');
		const body = calls[0].init.body as FormData;
		expect(calls[0].url).toBe('/api/recipes/import/files');
		expect(body.getAll('files').map((f) => (f as File).name)).toEqual(['page1.jpg', 'card.pdf']);
		expect(body.get('text')).toBe('serves 4');
		expect((calls[0].init.headers as Record<string, string>)['content-type']).toBeUndefined();
	});

	it('deletes without reading a body, and surfaces a stale save as 409', async () => {
		const calls = serve(204);
		await expect(api.recipesDelete('recipes/a.md')).resolves.toBeUndefined();
		expect([calls[0].init.method, calls[0].url]).toEqual(['DELETE', '/api/recipes/item/recipes/a.md']);

		serve(409, { detail: 'The recipe changed since you opened it' });
		const failure = api.recipesUpdate('recipes/a.md', emptyRecipe(), 'old');
		await expect(failure).rejects.toBeInstanceOf(ApiError);
		await expect(failure).rejects.toMatchObject({ status: 409 });
	});
});
