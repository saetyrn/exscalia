// The tab strip's list: what survives a reload, where the highlight goes when
// a tab closes or a navigation lands on a URL another tab holds, and how tabs
// follow notes that are renamed or trashed. load() runs when the module is
// evaluated, so every case boots a fresh copy over the storage it sets up.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { emptyRecipe } from './recipes/types';

const goto = vi.hoisted(() => vi.fn(async () => {}));
vi.mock('$app/navigation', () => ({ goto }));

const storage = new Map<string, string>();
vi.stubGlobal('localStorage', {
	getItem: (key: string) => storage.get(key) ?? null,
	setItem: (key: string, value: string) => void storage.set(key, value),
	removeItem: (key: string) => void storage.delete(key)
});

type TabsModule = typeof import('./tabs.svelte');

/** Tabs t0, t1, … on the given URLs, persisted the way persist() writes them. */
const saved = (active: string, ...urls: string[]) => ({
	version: 1,
	active,
	tabs: urls.map((url, i) => ({ id: `t${i}`, url }))
});

async function boot(state?: unknown): Promise<TabsModule> {
	storage.clear();
	if (state !== undefined) storage.set('exscalia.tabs', JSON.stringify(state));
	vi.resetModules();
	return import('./tabs.svelte');
}

const urls = (mod: TabsModule) => mod.tabs.list.map((tab) => tab.url);
const persisted = () => JSON.parse(storage.get('exscalia.tabs') ?? 'null');

beforeEach(() => {
	goto.mockClear();
});

describe('load', () => {
	it('starts over with one home tab when the saved list is not usable', async () => {
		for (const state of [
			{ ...saved('t0', '/note/a.md'), version: 2 },
			{ version: 1, active: 't0', tabs: 'nope' },
			{ version: 1, active: 't0', tabs: [{ id: 't0', url: 'https://elsewhere.test/' }] },
			{ version: 1, active: 't0', tabs: [{ id: 5, url: '/note/a.md' }] },
			saved('t0')
		]) {
			const mod = await boot(state);
			expect(urls(mod)).toEqual(['/']);
			expect(mod.tabs.activeId).toBe(mod.tabs.list[0].id);
		}
		storage.set('exscalia.tabs', '{not json');
		vi.resetModules();
		expect(urls(await import('./tabs.svelte'))).toEqual(['/']);
	});

	it('restores the saved active tab, or the first when it is gone', async () => {
		const mod = await boot(saved('t1', '/', '/note/a.md', '/calendar'));
		expect(urls(mod)).toEqual(['/', '/note/a.md', '/calendar']);
		expect(mod.tabs.activeId).toBe('t1');

		const orphaned = await boot(saved('t9', '/note/a.md', '/calendar'));
		expect(orphaned.tabs.activeId).toBe('t0');
	});

	it('keeps a tab whose URL does not decode, and titles it', async () => {
		// Cmd-clicking a markdown link like [x](/note/50%.md) stores the raw
		// href. Its title threw URIError inside the layout on every load.
		const mod = await boot(
			saved('t0', '/', '/note/50%.md', '/tasks/a%zz', '/media/x%.md', '/tags/100%')
		);
		expect(urls(mod)).toHaveLength(5);
		expect(mod.routeTitle('/note/50%.md')).toBe('50%');
		expect(mod.routeTitle('/tasks/a%zz')).toBe('Tasks · a%zz');
		expect(mod.routeTitle('/media/x%.md')).toBe('x%');
		expect(mod.routeTitle('/tags/100%')).toBe('#100%');
		expect(mod.routeTitle('/note/a/Caf%C3%A9.md')).toBe('Café');
	});

	it('titles the recipe screens', async () => {
		const mod = await boot();
		expect(mod.routeTitle('/recipes')).toBe('Recipes');
		expect(mod.routeTitle('/recipes/add')).toBe('New recipe');
		expect(mod.routeTitle('/recipes/review')).toBe('Review draft');
		expect(mod.routeTitle('/recipes/recipes/cr%C3%A8me-br%C3%BBl%C3%A9e.md')).toBe('crème-brûlée');
		expect(mod.routeTitle('/recipes/edit/recipes/pie.md')).toBe('Edit · pie');
		expect(mod.routeTitle('/recipes/recipes/50%.md')).toBe('50%');
	});

	it('titles a recipe by its title once seen, since a rename keeps the file', async () => {
		const mod = await boot();
		const cache = await import('./recipes/cache.svelte');
		cache.rememberRecipe({
			...emptyRecipe(),
			title: 'Roast "pie" #2?',
			path: 'recipes/pie.md',
			revision: 'r1',
			created: '2026-10-04T12:00:00Z',
			modified: '2026-10-04T12:00:00Z',
			art: '',
			art_colors: '',
			art_palette: {}
		});
		expect(mod.routeTitle('/recipes/recipes/pie.md')).toBe('Roast "pie" #2?');
		expect(mod.routeTitle('/recipes/edit/recipes/pie.md')).toBe('Edit · Roast "pie" #2?');
		expect(mod.routeTitle('/recipes/recipes/tart.md')).toBe('tart');
	});
});

describe('closeTab', () => {
	it('moves to the right neighbour, then to the new last tab', async () => {
		const mod = await boot(saved('t1', '/note/a.md', '/note/b.md', '/note/c.md'));

		mod.closeTab('t1');
		expect(urls(mod)).toEqual(['/note/a.md', '/note/c.md']);
		expect(mod.tabs.activeId).toBe('t2');
		expect(goto).toHaveBeenLastCalledWith('/note/c.md');

		mod.closeTab('t2');
		expect(urls(mod)).toEqual(['/note/a.md']);
		expect(mod.tabs.activeId).toBe('t0');
		expect(goto).toHaveBeenLastCalledWith('/note/a.md');
		expect(persisted().active).toBe('t0');
	});

	it('closes a background tab without navigating', async () => {
		const mod = await boot(saved('t0', '/note/a.md', '/note/b.md'));
		mod.closeTab('t1');
		expect(urls(mod)).toEqual(['/note/a.md']);
		expect(mod.tabs.activeId).toBe('t0');
		expect(goto).not.toHaveBeenCalled();
	});

	it('opens a fresh home tab when the last one closes', async () => {
		const mod = await boot(saved('t0', '/note/a.md'));
		mod.closeTab('t0');
		expect(urls(mod)).toEqual(['/']);
		expect(mod.tabs.activeId).toBe(mod.tabs.list[0].id);
		expect(goto).toHaveBeenLastCalledWith('/');
	});
});

describe('syncActiveTab', () => {
	it('moves the highlight to a tab already showing the URL', async () => {
		const mod = await boot(saved('t0', '/note/a.md', '/note/b.md'));
		mod.syncActiveTab('/note/b.md');
		expect(mod.tabs.activeId).toBe('t1');
		// The tab navigated away from keeps what it showed.
		expect(urls(mod)).toEqual(['/note/a.md', '/note/b.md']);
	});

	it('otherwise writes the URL into the active tab', async () => {
		const mod = await boot(saved('t0', '/note/a.md', '/note/b.md'));
		mod.syncActiveTab('/calendar');
		expect(mod.tabs.activeId).toBe('t0');
		expect(urls(mod)).toEqual(['/calendar', '/note/b.md']);
		expect(persisted().tabs[0].url).toBe('/calendar');
	});
});

describe('reorderTab', () => {
	it('clamps the gap to the ends of the list', async () => {
		const mod = await boot(saved('t0', '/1', '/2', '/3'));
		mod.reorderTab('t0', 99);
		expect(mod.tabs.list.map((tab) => tab.id)).toEqual(['t1', 't2', 't0']);
		mod.reorderTab('t0', -4);
		expect(mod.tabs.list.map((tab) => tab.id)).toEqual(['t0', 't1', 't2']);
		mod.reorderTab('t2', 1);
		expect(persisted().tabs.map((tab: { id: string }) => tab.id)).toEqual(['t0', 't2', 't1']);
	});
});

describe('retargetTabs', () => {
	it('follows a renamed note', async () => {
		const mod = await boot(saved('t0', '/note/a/One.md', '/note/a/One.md.bak', '/note/b/One.md'));
		mod.retargetTabs('a/One.md', 'a/50% Uno.md');
		expect(urls(mod)).toEqual([
			'/note/a/50%25%20Uno.md',
			'/note/a/One.md.bak',
			'/note/b/One.md'
		]);
		expect(persisted().tabs[0].url).toBe('/note/a/50%25%20Uno.md');
	});

	it('follows a moved folder into every note and board under it', async () => {
		const mod = await boot(
			saved(
				't1',
				'/note/a/One.md',
				'/note/a/Sub%20Dir/Two.md?view=raw#top',
				'/tasks/a',
				'/tasks/a/deeper',
				'/note/ab/Three.md',
				'/calendar'
			)
		);
		mod.retargetTabs('a', 'z z');
		expect(urls(mod)).toEqual([
			'/note/z%20z/One.md',
			'/note/z%20z/Sub%20Dir/Two.md?view=raw#top',
			'/tasks/z%20z',
			'/tasks/z%20z/deeper',
			'/note/ab/Three.md',
			'/calendar'
		]);
		expect(mod.tabs.activeId).toBe('t1');
	});
});

describe('closeTabsUnder', () => {
	it('closes background tabs on a trashed note or inside a trashed folder', async () => {
		const mod = await boot(
			saved(
				't1',
				'/note/a/One.md',
				'/note/a/Two.md',
				'/tasks/a',
				'/note/ab/Three.md',
				'/calendar'
			)
		);
		mod.closeTabsUnder('a');
		// The active tab stays for the caller to navigate away.
		expect(urls(mod)).toEqual(['/note/a/Two.md', '/note/ab/Three.md', '/calendar']);
		expect(mod.tabs.activeId).toBe('t1');
		expect(persisted().tabs).toHaveLength(3);

		mod.closeTabsUnder('ab/Three.md');
		expect(urls(mod)).toEqual(['/note/a/Two.md', '/calendar']);
		expect(goto).not.toHaveBeenCalled();
	});
});
