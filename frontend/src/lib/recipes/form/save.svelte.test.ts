// Saving: the photo import's card goes up through the attachments route
// once, a failed upload saves without one, a link's remote thumbnail goes
// through untouched, and an update sends the loaded revision and no art,
// going again when a drawing that landed meanwhile is all that changed.
// Also the hand-off's reload behaviour.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '$lib/api';
import type { Recipe, RecipeIn } from '../types';
import { emptyRecipe } from '../types';
import { cachedRecipe } from '../cache.svelte';
import { clearDraft, readDraft, setDraft } from './handoff.svelte';
import { saveEdit, saveNew } from './save';

const stub = vi.hoisted(() => ({
	recipesCreate: vi.fn(),
	recipesUpdate: vi.fn(),
	recipesGet: vi.fn(),
	uploadAttachment: vi.fn()
}));

vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return {
		...actual,
		uploadAttachment: stub.uploadAttachment,
		api: {
			...actual.api,
			recipesCreate: stub.recipesCreate,
			recipesUpdate: stub.recipesUpdate,
			recipesGet: stub.recipesGet
		}
	};
});

const storage = new Map<string, string>();
vi.stubGlobal('sessionStorage', {
	getItem: (key: string) => storage.get(key) ?? null,
	setItem: (key: string, value: string) => void storage.set(key, value),
	removeItem: (key: string) => void storage.delete(key)
});

const THUMB = 'data:image/jpeg;base64,/9j/AA==';

const stored = (recipe: RecipeIn, path = 'recipes/soup.md'): Recipe => ({
	art: '',
	art_colors: '',
	art_palette: {},
	...recipe,
	path,
	revision: 'r2',
	created: '2026-10-04T12:00:00Z',
	modified: '2026-10-04T12:00:00Z'
});

beforeEach(() => {
	vi.clearAllMocks();
	storage.clear();
	clearDraft();
	stub.recipesCreate.mockImplementation(async (recipe: RecipeIn) => stored(recipe));
	stub.recipesUpdate.mockImplementation(async (path: string, recipe: RecipeIn) => stored(recipe, path));
});

const photoRecipe = (): RecipeIn => ({
	...emptyRecipe(),
	title: 'Soup',
	source: { type: 'photo', url: null, thumbnail: null }
});

describe('saving a new recipe', () => {
	it('uploads the first photo once and sets it as the card', async () => {
		stub.uploadAttachment.mockResolvedValue({ path: 'attachments/recipe-1.jpg', size: 4, url: '/api/attachments/recipe-1.jpg' });
		setDraft({ recipe: photoRecipe(), missing: [], method: 'ai' }, THUMB);
		stub.recipesCreate.mockRejectedValueOnce(new Error('offline'));
		await expect(saveNew(photoRecipe(), readDraft())).rejects.toThrow('offline');
		const saved = await saveNew(photoRecipe(), readDraft());

		expect(stub.uploadAttachment).toHaveBeenCalledTimes(1);
		const upload: File = stub.uploadAttachment.mock.calls[0][0];
		expect([upload.name, upload.type]).toEqual(['recipe.jpg', 'image/jpeg']);
		expect(stub.recipesCreate).toHaveBeenLastCalledWith({
			...photoRecipe(),
			source: { type: 'photo', url: null, thumbnail: '/api/attachments/recipe-1.jpg' }
		});
		expect(cachedRecipe(saved.path)).toEqual(saved);
	});

	it('saves without a card when the upload fails', async () => {
		stub.uploadAttachment.mockRejectedValue(new Error('too large'));
		setDraft({ recipe: photoRecipe(), missing: [], method: 'ai' }, THUMB);
		await saveNew(photoRecipe(), readDraft());
		expect(stub.recipesCreate).toHaveBeenCalledWith(photoRecipe());
	});

	it('passes a link’s remote thumbnail through and uploads nothing', async () => {
		const linked: RecipeIn = {
			...emptyRecipe(),
			title: 'Pie',
			source: { type: 'website', url: 'https://example.com/pie', thumbnail: 'https://example.com/pie.jpg' }
		};
		setDraft({ recipe: linked, missing: [], method: 'schema_org' });
		await saveNew(linked, readDraft());
		expect(stub.uploadAttachment).not.toHaveBeenCalled();
		expect(stub.recipesCreate).toHaveBeenCalledWith(linked);
	});

	it('typed with no draft: just creates', async () => {
		await saveNew({ ...emptyRecipe(), title: 'Toast' }, null);
		expect(stub.uploadAttachment).not.toHaveBeenCalled();
		expect(stub.recipesCreate).toHaveBeenCalledWith({ ...emptyRecipe(), title: 'Toast' });
	});
});

describe('saving an edit', () => {
	const loaded = (): Recipe => ({ ...stored({ ...emptyRecipe(), title: 'Soup' }), revision: 'r1' });
	const stale = () => Promise.reject(new ApiError(409, 'Stale'));

	it('sends the loaded revision and keeps the cache', async () => {
		const recipe: RecipeIn = { ...emptyRecipe(), title: 'Soup' };
		const saved = await saveEdit('recipes/soup.md', recipe, loaded());
		expect(stub.recipesUpdate).toHaveBeenCalledWith('recipes/soup.md', recipe, 'r1');
		expect('art' in stub.recipesUpdate.mock.calls[0][1]).toBe(false);
		expect(cachedRecipe('recipes/soup.md')).toEqual(saved);
	});

	it('goes again when only a drawing landed since the form loaded', async () => {
		const edit: RecipeIn = { ...emptyRecipe(), title: 'Soup', notes: 'Typed meanwhile.' };
		stub.recipesUpdate.mockImplementationOnce(stale);
		stub.recipesGet.mockResolvedValue({ ...loaded(), revision: 'r2', art: ' () ', art_palette: { a: '#ffffff' } });
		const saved = await saveEdit('recipes/soup.md', edit, loaded());
		expect(stub.recipesUpdate.mock.calls.map((call) => call[2])).toEqual(['r1', 'r2']);
		expect(saved.notes).toBe('Typed meanwhile.');
	});

	it('is a conflict when the recipe itself changed elsewhere', async () => {
		const edit: RecipeIn = { ...emptyRecipe(), title: 'Soup', notes: 'Mine.' };
		stub.recipesUpdate.mockImplementationOnce(stale);
		stub.recipesGet.mockResolvedValue({ ...loaded(), revision: 'r2', notes: 'Theirs.' });
		await expect(saveEdit('recipes/soup.md', edit, loaded())).rejects.toMatchObject({ status: 409 });
		expect(stub.recipesUpdate).toHaveBeenCalledTimes(1);
	});
});

describe('the draft hand-off', () => {
	it('survives a reload through this tab’s storage, and not a clear', async () => {
		setDraft({ recipe: photoRecipe(), missing: ['No servings'], method: 'ai' }, THUMB);
		expect(JSON.parse(storage.get('exscalia:recipes:draft')!).draft.recipe.title).toBe('Soup');

		// A reload: the module starts over and reads the stored copy.
		vi.resetModules();
		const fresh = await import('./handoff.svelte');
		expect(fresh.readDraft()).toEqual({
			draft: { recipe: photoRecipe(), missing: ['No servings'], method: 'ai' },
			thumb: THUMB,
			thumbUrl: null
		});
		fresh.clearDraft();
		expect(storage.has('exscalia:recipes:draft')).toBe(false);
	});

	it('ignores a stored copy it cannot read', async () => {
		storage.set('exscalia:recipes:draft', '{"draft": 3}');
		vi.resetModules();
		expect((await import('./handoff.svelte')).readDraft()).toBeNull();
		storage.set('exscalia:recipes:draft', 'not json');
		vi.resetModules();
		expect((await import('./handoff.svelte')).readDraft()).toBeNull();
	});
});
