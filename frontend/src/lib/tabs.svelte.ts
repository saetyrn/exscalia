// Navigation tabs, Chrome-style: each tab is just a pointer {id, url} and
// only the active tab's view is ever mounted — switching tabs navigates.
// The list is per-device state (localStorage), never part of the account.
//
// The afterNavigate hook in +layout.svelte calls syncActiveTab() after every
// navigation, so the active tab always holds the real URL. activateTab sets
// activeId BEFORE calling goto — that ordering is the entire reentrancy
// guard: the navigation's own syncActiveTab writes the URL into the tab that
// already carries it.

import { goto } from '$app/navigation';
import { encodePath } from './api';
import { notebookTitleFromPath } from './paperurl';

export interface Tab {
	id: string;
	url: string;
}

const KEY = 'exscalia.tabs';
const VERSION = 1;

function newId(): string {
	return typeof crypto !== 'undefined' && 'randomUUID' in crypto
		? crypto.randomUUID()
		: `t${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
}

/** Anything malformed — bad version, bad shape, empty list — resets to a
 * single tab at home. A tab whose label cannot be drawn is dropped: TabStrip
 * renders every label inside the root layout, so one bad persisted URL would
 * otherwise break every load until storage was cleared by hand. */
function load(): { list: Tab[]; activeId: string } {
	try {
		if (typeof localStorage !== 'undefined') {
			const raw = JSON.parse(localStorage.getItem(KEY) ?? 'null');
			if (raw?.version === VERSION && Array.isArray(raw.tabs)) {
				const list = raw.tabs.filter(
					(t: unknown): t is Tab =>
						!!t &&
						typeof (t as Tab).id === 'string' &&
						typeof (t as Tab).url === 'string' &&
						(t as Tab).url.startsWith('/') &&
						drawable((t as Tab).url)
				);
				if (list.length) {
					const activeId = list.some((t: Tab) => t.id === raw.active)
						? (raw.active as string)
						: list[0].id;
					return { list, activeId };
				}
			}
		}
	} catch {
		// fall through to the fresh-tab default
	}
	const tab = { id: newId(), url: '/' };
	return { list: [tab], activeId: tab.id };
}

function drawable(url: string): boolean {
	try {
		routeTitle(url);
		return true;
	} catch {
		return false;
	}
}

const initial = load();
export const tabs = $state({ list: initial.list, activeId: initial.activeId });

function persist(): void {
	try {
		localStorage.setItem(
			KEY,
			JSON.stringify({
				version: VERSION,
				active: tabs.activeId,
				tabs: tabs.list.map(({ id, url }) => ({ id, url }))
			})
		);
	} catch {
		// Storage unavailable — tabs still work for this session.
	}
}

export function activateTab(id: string): void {
	const tab = tabs.list.find((t) => t.id === id);
	if (!tab || tabs.activeId === id) return;
	tabs.activeId = id; // before goto — see module comment
	persist();
	void goto(tab.url);
}

export function newTab(url = '/'): void {
	const tab = { id: newId(), url };
	tabs.list.push(tab);
	tabs.activeId = tab.id;
	persist();
	void goto(url);
}

/** Cmd/middle-click target: appended at the end, no navigation. A note
 * already open in some tab doesn't get a twin — the point of a background
 * open is "have it ready", and it already is. */
export function openBackgroundTab(url: string): void {
	if (tabs.list.some((t) => t.url === url)) return;
	tabs.list.push({ id: newId(), url });
	persist();
}

export function closeTab(id: string): void {
	const at = tabs.list.findIndex((t) => t.id === id);
	if (at < 0) return;
	const wasActive = tabs.list[at].id === tabs.activeId;
	tabs.list.splice(at, 1);
	if (tabs.list.length === 0) {
		// Closing the only tab is an explicit "start over".
		const tab = { id: newId(), url: '/' };
		tabs.list.push(tab);
		tabs.activeId = tab.id;
		persist();
		void goto('/');
		return;
	}
	if (wasActive) {
		// Right neighbor (now at the same index), else the new last tab.
		const next = tabs.list[Math.min(at, tabs.list.length - 1)];
		tabs.activeId = next.id;
		persist();
		void goto(next.url);
		return;
	}
	persist();
}

/** Move a tab into a gap measured against the list with that tab removed. */
export function reorderTab(id: string, gapIndex: number): void {
	const from = tabs.list.findIndex((tab) => tab.id === id);
	if (from < 0) return;
	const [moving] = tabs.list.splice(from, 1);
	const at = Math.max(0, Math.min(gapIndex, tabs.list.length));
	tabs.list.splice(at, 0, moving);
	persist();
}

/** The vault path a /note/ or /tasks/ URL points at, with the route prefix
 * and whatever follows the path (query, hash) — null for any other route. */
function vaultTarget(url: string): { route: string; path: string; rest: string } | null {
	const cut = url.search(/[?#]/);
	const pathname = cut < 0 ? url : url.slice(0, cut);
	for (const route of ['/note/', '/tasks/']) {
		if (pathname.startsWith(route)) {
			return {
				route,
				path: safeDecode(pathname.slice(route.length)),
				rest: cut < 0 ? '' : url.slice(cut)
			};
		}
	}
	return null;
}

const within = (path: string, root: string) => path === root || path.startsWith(`${root}/`);

/** A note or folder was renamed or moved: tabs on it, or on anything inside
 * it (notes under a folder, the folder's board), follow it. Left alone they
 * opened to a not-found page the next time they were picked. */
export function retargetTabs(oldPath: string, newPath: string): void {
	if (!oldPath || oldPath === newPath) return;
	let changed = false;
	for (const tab of tabs.list) {
		const target = vaultTarget(tab.url);
		if (!target || !within(target.path, oldPath)) continue;
		const moved = newPath + target.path.slice(oldPath.length);
		tab.url = `${target.route}${encodePath(moved)}${target.rest}`;
		changed = true;
	}
	if (changed) persist();
}

/** A note or folder went to the Trash: close the background tabs on it or
 * inside it. The active tab stays — the caller is navigating it away, and
 * an empty strip is never an option. */
export function closeTabsUnder(path: string): void {
	if (!path) return;
	const kept = tabs.list.filter((tab) => {
		if (tab.id === tabs.activeId) return true;
		const target = vaultTarget(tab.url);
		return !target || !within(target.path, path);
	});
	if (kept.length === tabs.list.length) return;
	tabs.list = kept;
	persist();
}

export function cycleTab(delta: 1 | -1): void {
	const at = tabs.list.findIndex((t) => t.id === tabs.activeId);
	const next = tabs.list[(at + delta + tabs.list.length) % tabs.list.length];
	if (next) activateTab(next.id);
}

/** Cmd+1..8 select by position, Cmd+9 the last tab (Chrome convention). */
export function jumpToTab(digit: number): void {
	const tab = digit === 9 ? tabs.list[tabs.list.length - 1] : tabs.list[digit - 1];
	if (tab) activateTab(tab.id);
}

/** afterNavigate sink: the active tab always shows the truth.
 *
 * With one twist: navigating to a URL some OTHER tab already holds switches
 * to that tab instead of duplicating it. The route is already showing the
 * URL, so adopting the twin is just moving the highlight — the tab you came
 * from keeps pointing at what it showed before. Deliberately not applied to
 * newTab/activateTab: both set the active tab's URL before their goto, so
 * they hit the early return and the + button can still open a second home
 * tab on purpose. */
export function syncActiveTab(url: string): void {
	const tab = tabs.list.find((t) => t.id === tabs.activeId);
	if (!tab || tab.url === url) return;
	const twin = tabs.list.find((t) => t.url === url);
	if (twin) {
		tabs.activeId = twin.id;
		persist();
		return;
	}
	tab.url = url;
	persist();
}

/** decodeURIComponent that hands back its input instead of throwing. Tab
 * URLs come from any internal link, a user's markdown `[x](/note/50%.md)`
 * included, and a URIError inside the layout's render breaks every page. */
export function safeDecode(value: string): string {
	try {
		return decodeURIComponent(value);
	} catch {
		return value;
	}
}

/** Route → human label, shared by tab labels and the mobile top bar. */
export function routeTitle(url: string): string {
	const path = url.split('?')[0].split('#')[0];
	if (path.startsWith('/note/')) {
		const raw = safeDecode(path.replace(/^\/note\//, '')).split('/').pop() ?? 'Note';
		return raw.replace(/\.md$/, '');
	}
	if (path === '/calendar') return 'Calendar';
	if (path.startsWith('/tasks/')) {
		const folder = safeDecode(path.slice('/tasks/'.length));
		return `Tasks · ${folder.split('/').pop() || folder}`;
	}
	if (path === '/tasks') return 'Tasks';
	if (path === '/media') return 'Media';
	if (path === '/media/library') return 'Library';
	if (path === '/media/activity') return 'Activity';
	if (path === '/media/watchlist') return 'Watchlist';
	if (path.startsWith('/media/')) {
		const raw = safeDecode(path.replace(/^\/media\//, '')).split('/').pop() ?? 'Media';
		return raw.replace(/\.md$/, '');
	}
	if (path === '/paper') return 'Paper';
	if (path.startsWith('/paper/')) {
		// Decoded per segment: an encoded slash inside a title is part of the
		// name, not a folder boundary.
		const notebook = path
			.slice('/paper/'.length)
			.split('/')
			.map((segment) => safeDecode(segment))
			.join('/');
		return `Paper · ${notebookTitleFromPath(notebook)}`;
	}
	if (path === '/trash') return 'Trash';
	if (path === '/history') return 'Version history';
	if (path === '/omnia' || path === '/overseer') return 'OMNIA';
	if (path === '/pending') return 'Pending changes';
	if (path === '/settings') return 'Settings';
	if (path === '/search') return 'Search';
	if (path.startsWith('/study/')) return 'Study';
	if (path.startsWith('/tags/')) return `#${safeDecode(path.replace(/^\/tags\//, ''))}`;
	return 'Notes';
}
