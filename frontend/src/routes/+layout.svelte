<script lang="ts">
	import { onMount } from 'svelte';
	import { afterNavigate, beforeNavigate, goto } from '$app/navigation';
	import { navigating, page, updated } from '$app/state';
	import { CLIENT_ID, connectLive, type LiveNoteEvent } from '$lib/live';
	import { consumeRouteKeyHold } from '$lib/routekey';
	import { sliding as recipeSliding } from '$lib/recipes/transition';
	import { cubicOut } from 'svelte/easing';
	import { fade, fly } from '$lib/motion';
	import '../app.css';
	import favicon from '$lib/assets/favicon.svg';
	import PendingBox from '$lib/PendingBox.svelte';
	import MediaAddDialog from '$lib/media/MediaAddDialog.svelte';
	import MobileTabBar from '$lib/MobileTabBar.svelte';
	import Sidebar from '$lib/Sidebar.svelte';
	import TabStrip from '$lib/TabStrip.svelte';
	import { openBackgroundTab, routeTitle, syncActiveTab } from '$lib/tabs.svelte';
	import UIHost from '$lib/UIHost.svelte';
	import { api } from '$lib/api';
	import { modalOpen } from '$lib/modal';
	import { parsePaperHash } from '$lib/paperurl';
	import {
		DEFAULT_SERVER,
		apiBase,
		getToken,
		isDesktop,
		isIPadShell,
		postNative,
		setServer,
		setToken
	} from '$lib/runtime';
	import { startPreload } from '$lib/preload.svelte';
	import { applyAppearanceSettings, loadLocalAppearance } from '$lib/theme';
	import { notify, openPalette, ui } from '$lib/ui.svelte';
	import {
		afterPathsMoved,
		omniaContext,
		refreshNotes,
		refreshOverseerStatus,
		refreshPendingCount,
		syncState,
		refreshTags,
		vault
	} from '$lib/stores.svelte';
	import BottomSheet from '$lib/BottomSheet.svelte';
	import Glyph from '$lib/Glyph.svelte';
	import { morning, refreshMorning, dismissMorning, localToday } from '$lib/morning.svelte';
	import { syncPush } from '$lib/push';
	import { unshownAlerts } from '$lib/alerts';
	import { semverLt, serverInfo } from '$lib/version';

	let { children } = $props();

	let authState: 'unknown' | 'ok' | 'login' = $state('unknown');
	// The session died under a running app: the login form covers the shell
	// instead of replacing it, so the open editor and its unsaved text survive.
	let reauth = $state(false);
	// Bumped after a re-login: the live loop gave up on the dead session for
	// good, so the live effect below tears it down and connects afresh.
	let liveGeneration = $state(0);
	let username = $state('');
	let password = $state('');
	let serverInput = $state((isDesktop && apiBase()) || DEFAULT_SERVER);
	let loginError: string | null = $state(null);
	let mobileNavOpen = $state(false);

	// The morning briefing is a top banner on desktop and a native bottom sheet
	// on mobile; this tracks the breakpoint so the layout picks the right host.
	let isMobile = $state(false);
	$effect(() => {
		const mq = window.matchMedia('(max-width: 760px)');
		isMobile = mq.matches;
		const onChange = (e: MediaQueryListEvent) => (isMobile = e.matches);
		mq.addEventListener('change', onChange);
		return () => mq.removeEventListener('change', onChange);
	});
	// The local day the briefing was last built for — a session left open past
	// midnight re-fetches and re-shows against the new date (see pollAlerts tick).
	let briefingDay = localToday();

	// The briefing renders OMNIA's reply box, which pulls in the markdown/KaTeX
	// renderer — too heavy for every route's first paint (login included) when
	// it shows at most once a day. Loaded the moment it is due; the banner and
	// sheet wait for it so they never open empty.
	let MorningBriefing: typeof import('$lib/MorningBriefing.svelte').default | null =
		$state(null);
	$effect(() => {
		if (!morning.visible || MorningBriefing) return;
		import('$lib/MorningBriefing.svelte')
			.then((mod) => (MorningBriefing = mod.default))
			.catch(() => {
				// Offline with a cold cache: no banner this time, like no data.
			});
	});

	// Study mode takes over the whole window — no sidebar, no mobile bar.
	let immersive = $derived(page.url.pathname.startsWith('/study/'));
	// Media routes slide the pane under a frosted tab strip (guarded on
	// immersive, where no strip renders to slide under).
	let mediaUnder = $derived(page.url.pathname.startsWith('/media') && !immersive);

	// The media look must also own the DOCUMENT background: iOS paints the
	// status-bar zone and the strip under the fixed tab bar from the page
	// root, and the app-theme color was bleeding through both on /media.
	$effect(() => {
		if (typeof document === 'undefined') return;
		document.documentElement.toggleAttribute('data-media-look', mediaUnder);
		return () => document.documentElement.removeAttribute('data-media-look');
	});

	// Recipes wear their own black the same way (lib/recipes/recipes.css).
	let recipesUnder = $derived(page.url.pathname.startsWith('/recipes') && !immersive);
	$effect(() => {
		if (typeof document === 'undefined') return;
		document.documentElement.toggleAttribute('data-recipes-look', recipesUnder);
		return () => document.documentElement.removeAttribute('data-recipes-look');
	});

	// The page normally remounts per pathname ({#key routeKey} below). A held
	// navigation (a rename moving the URL under the open note) keeps the key,
	// so the mounted page absorbs the change in place instead of blanking.
	//
	// The four media-hub tabs collapse to a single key: they share the
	// (hub)/+layout shell, so keeping the key stable across them stops the root
	// {#key} from remounting the whole shell on every tab tap — the hub then
	// swaps only its own content (with its own slide). The item route
	// (/media/media/*) is a different layout group and keeps its own key.
	const HUB_PATHS = new Set([
		'/media',
		'/media/library',
		'/media/activity',
		'/media/watchlist'
	]);
	function keyForPath(pathname: string): string {
		return HUB_PATHS.has(pathname) ? 'media-hub' : pathname;
	}
	let routeKey = $state(keyForPath(page.url.pathname));
	$effect(() => {
		const nextKey = keyForPath(page.url.pathname);
		if (nextKey === routeKey) return;
		// The key is an opaque token, not a mirror of the URL: on a held
		// navigation it simply keeps its old value, so the keyed block —
		// and the page inside it — survives untouched.
		if (consumeRouteKeyHold()) return;
		routeKey = nextKey;
	});

	// Mobile route entrance: a subtle fade + short upward slide (in-only —
	// an out transition would keep the old page in flow and shift layout).
	// Desktop keeps the flat fade; reduced motion collapses to opacity only.
	function routeIntro(_node: Element) {
		const reduce =
			typeof window !== 'undefined' &&
			window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		const mobile =
			typeof window !== 'undefined' &&
			window.matchMedia('(max-width: 760px)').matches;
		if (reduce) return { duration: 0, css: (t: number) => `opacity: ${t}` };
		// A recipes page slide is the entrance; a fade inside it would show.
		if (recipeSliding()) return { duration: 0 };
		if (!mobile)
			return { duration: 140, easing: cubicOut, css: (t: number) => `opacity: ${t}` };
		return {
			duration: 180,
			easing: cubicOut,
			css: (t: number, u: number) => `opacity: ${t}; transform: translateY(${10 * u}px)`
		};
	}

	// Every navigation — link, goto, redirect, initial load — lands here, so
	// the active tab always holds the URL actually being shown.
	afterNavigate(() => {
		syncActiveTab(page.url.pathname + page.url.search + page.url.hash);
	});

	// --- iPad shell bridge ---------------------------------------------------
	// Inside the native app /paper is not a web page at all: the notebooks are
	// drawn with a Pencil in a native section that the WKWebView sits beside.
	// Every route into it — the sidebar link, the command palette, a search
	// hit, an OMNIA navigate — is a plain client-side navigation, so cancelling
	// them all takes one hook rather than a special case per caller.
	function paperHandoff(url: URL): {
		type: 'open';
		section: 'paper';
		notebook?: string;
		notebook_id?: string;
		page?: string;
		region?: string;
		folder?: string;
	} {
		const stable = url.pathname.match(/^\/paper\/id\/([^/]+)\/([^/]+)$/);
		if (stable) {
			const decode = (value: string) => {
				try {
					return decodeURIComponent(value);
				} catch {
					return value;
				}
			};
			const region = url.searchParams.get('region');
			return {
				type: 'open',
				section: 'paper',
				notebook_id: decode(stable[1]),
				page: decode(stable[2]),
				...(region ? { region } : {})
			};
		}
		const notebook = url.pathname
			.slice('/paper'.length)
			.replace(/^\//, '')
			.split('/')
			.filter(Boolean)
			.map((segment) => {
				try {
					return decodeURIComponent(segment);
				} catch {
					return segment;
				}
			})
			.join('/');
		const page = parsePaperHash(url.hash);
		// `/paper?folder=paper/Work`: the native library opens that folder.
		const folder = url.searchParams.get('folder');
		return {
			type: 'open',
			section: 'paper',
			...(notebook ? { notebook } : {}),
			...(page ? { page } : {}),
			...(!notebook && folder ? { folder } : {})
		};
	}

	beforeNavigate((nav) => {
		if (!isIPadShell) return;
		if (!nav.to?.url.pathname.startsWith('/paper')) return;
		nav.cancel();
		postNative(paperHandoff(nav.to.url));
	});

	onMount(() => {
		if (!isIPadShell) return;
		// Native → web deep links (an OMNIA answer opening a note from the
		// Paper section). The shell only ever passes an app path.
		(window as unknown as { __exscaliaNavigate?: (path: string) => void }).__exscaliaNavigate = (
			path: string
		) => {
			if (typeof path === 'string' && path.startsWith('/')) void goto(path);
		};
		// A cold launch straight onto /paper (a restored tab, a shared link)
		// never runs beforeNavigate — hand off, then park the web view on the
		// home route so returning from Paper lands somewhere real.
		if (page.url.pathname.startsWith('/paper')) {
			postNative(paperHandoff(page.url));
			void goto('/');
		}
	});

	// Cmd/Ctrl+click and middle-click on any internal link open it in a new
	// background tab. Delegated + capture-phase so one handler covers the
	// sidebar, read-mode wikilinks ({@html} anchors), search results, and tag
	// chips — and beats both SvelteKit's router (which ignores modifier
	// clicks) and the browser's open-a-real-browser-tab default.
	$effect(() => {
		const internalHref = (e: MouseEvent): string | null => {
			const anchor = (e.target as Element | null)?.closest?.('a[href]');
			if (!anchor || anchor.getAttribute('target') === '_blank') return null;
			const href = anchor.getAttribute('href');
			return href?.startsWith('/') ? href : null;
		};
		const onClick = (e: MouseEvent) => {
			if (!(e.metaKey || e.ctrlKey) || e.button !== 0) return;
			const href = internalHref(e);
			if (!href) return;
			e.preventDefault();
			e.stopPropagation();
			openBackgroundTab(href);
		};
		const onAux = (e: MouseEvent) => {
			if (e.button !== 1) return;
			const href = internalHref(e);
			if (!href) return;
			e.preventDefault();
			e.stopPropagation();
			openBackgroundTab(href);
		};
		window.addEventListener('click', onClick, true);
		window.addEventListener('auxclick', onAux, true);
		return () => {
			window.removeEventListener('click', onClick, true);
			window.removeEventListener('auxclick', onAux, true);
		};
	});

	// Mod-J opens OMNIA; add Shift and it picks the last conversation back up
	// instead of starting a fresh one (/omnia opens a blank chat by default,
	// and `?resume` is how the page is told otherwise).
	//
	// Capture phase, and no guard on where focus is: a modified chord never
	// types text, so it must also work from inside the editor — the same
	// reasoning (and the same phase) as the sidebar's Mod-M.
	$effect(() => {
		const onKeydown = (e: KeyboardEvent) => {
			if (!(e.metaKey || e.ctrlKey) || e.altKey || e.repeat) return;
			if (e.key.toLowerCase() !== 'j') return;
			e.preventDefault();
			e.stopPropagation();
			void goto(e.shiftKey ? '/omnia?resume=1' : '/omnia');
		};
		window.addEventListener('keydown', onKeydown, true);
		return () => window.removeEventListener('keydown', onKeydown, true);
	});

	let appearanceSynced = false;

	// Navigation now waits for route data, so slow loads would otherwise look
	// frozen. After 150ms of waiting, show a thin sweep at the top — fast
	// navigations (the normal case) never see it.
	let navSlow = $state(false);
	$effect(() => {
		if (navigating.to) {
			const timer = setTimeout(() => (navSlow = true), 150);
			return () => clearTimeout(timer);
		}
		navSlow = false;
	});

	// Reminders the user set on their own events, delivered at their real
	// trigger time. Polled while the app is open — there is no push channel,
	// and this is the only unprompted thing OMNIA does besides the greeting.
	// The server keeps each alert until it is acked here, so a response lost
	// in flight is re-sent on the next poll instead of vanishing.
	async function pollAlerts() {
		try {
			const { alerts } = await api.omniaAlerts();
			for (const alert of unshownAlerts(alerts)) {
				// OMNIA's own notifications (a briefing summary, a flag) carry no
				// event time — show them plainly, not as "started 3:04 PM".
				if (alert.source === 'omnia' || !alert.starts_at) {
					notify(alert.body ? `${alert.title} — ${alert.body}` : alert.title, {
						actionLabel: 'Open',
						action: () => goto('/omnia')
					});
					continue;
				}
				const at = new Date(alert.starts_at);
				const clock = at.toLocaleTimeString(undefined, {
					hour: 'numeric',
					minute: '2-digit'
				});
				notify(
					alert.overdue
						? `${alert.title} — started ${clock}`
						: `${alert.title} — in ${alert.minutes_before} min (${clock})`,
					{ actionLabel: 'Calendar', action: () => goto('/calendar') }
				);
			}
			// Everything returned has been shown this session, including any
			// whose earlier ack failed.
			if (alerts.length) await api.ackAlerts(alerts.map((alert) => alert.key));
		} catch {
			// Offline or not signed in yet; the next tick tries again.
		}
	}

	// Desktop sidebar collapse (mobile uses the drawer instead). Persisted.
	let sidebarHidden = $state(readSidebarHidden());

	function readSidebarHidden(): boolean {
		try {
			return localStorage.getItem('omnia.sidebarHidden') === '1';
		} catch {
			return false; // No storage (blocked, or no browser): shown.
		}
	}

	function toggleSidebar() {
		sidebarHidden = !sidebarHidden;
		try {
			localStorage.setItem('omnia.sidebarHidden', sidebarHidden ? '1' : '0');
		} catch {
			// Still applies for this page when storage is unavailable.
		}
	}

	// The command palette's "Toggle sidebar".
	$effect(() => {
		window.addEventListener('exscalia:toggle-sidebar', toggleSidebar);
		return () => window.removeEventListener('exscalia:toggle-sidebar', toggleSidebar);
	});

	// Mobile-bar only: the home route titles "Home" here while the desktop
	// TabStrip keeps routeTitle's mapping untouched.
	let mobileTitle = $derived(page.url.pathname === '/' ? 'Home' : routeTitle(page.url.pathname));

	// Desktop: this build's version, sent with the status call so the server
	// can log which builds still call in.
	async function desktopVersion(): Promise<string | null> {
		if (!isDesktop) return null;
		try {
			const { getVersion } = await import('@tauri-apps/api/app');
			return await getVersion();
		} catch {
			return null;
		}
	}

	// Desktop: below the server's declared minimum (raised only with a breaking
	// API change), the update offer stays up instead of fading.
	let updateRequired = false;
	function requireUpdate(): void {
		if (updateRequired) return;
		updateRequired = true;
		notify('Update required', {
			actionLabel: 'Update',
			action: () => void import('$lib/updater-desktop').then((mod) => mod.checkForUpdates()),
			timeout: 0
		});
	}

	$effect(() => {
		void desktopVersion().then((version) =>
			api
				.authStatus(version ? `desktop/${version}` : undefined)
				.then((s) => {
					serverInfo.apiVersion = s.api_version ?? null;
					const min = s.min_client?.desktop;
					if (version && min && semverLt(version, min)) requireUpdate();
					authState = s.authenticated ? 'ok' : 'login';
				})
				.catch((err) => {
					// Web: a backend without auth wiring — don't lock out. Desktop: an
					// unreachable server means show the login/server screen, not a
					// blank shell.
					if (isDesktop && !getToken()) authState = 'login';
					else if (isDesktop && (err as { status?: number }).status === 401) authState = 'login';
					else authState = 'ok';
				})
		);
	});

	// The session expired or was revoked from another device mid-session: any
	// 401 (api.ts) or the live stream giving up lands here. Fires repeatedly
	// while signed out; setting the same state again is harmless.
	$effect(() => {
		const expired = () => {
			void setToken(null); // desktop: drop the dead keychain token; web: no-op
			if (authState === 'ok') reauth = true;
			else authState = 'login';
		};
		window.addEventListener('exscalia:auth-expired', expired);
		return () => window.removeEventListener('exscalia:auth-expired', expired);
	});

	// Desktop: authenticated <img> attachments need blob URLs.
	$effect(() => {
		if (!isDesktop) return;
		let stop: (() => void) | undefined;
		void import('$lib/attachments-desktop').then((mod) => {
			stop = mod.watchAttachmentImages();
		});
		return () => stop?.();
	});

	// Desktop: one update check per launch, once the app is usable.
	let updateChecked = false;
	$effect(() => {
		if (!isDesktop || authState !== 'ok' || updateChecked) return;
		updateChecked = true;
		void import('$lib/updater-desktop').then((mod) => mod.checkForUpdates());
	});

	// Web only: cache the app shell so a reload without a connection still
	// opens the app. The desktop build has the real sync engine instead.
	$effect(() => {
		if (isDesktop || !('serviceWorker' in navigator) || !window.isSecureContext) return;
		navigator.serviceWorker.register('/service-worker.js').catch(() => {
			// A failed registration just means no offline shell — never fatal.
		});
		// Stale open windows are refreshed by the worker itself on activation
		// (see service-worker.ts) — that path also reaches pages whose bundle
		// predates any client-side listener, which this one could not.
	});

	$effect(() => {
		if (authState === 'ok') {
			if (!appearanceSynced) {
				appearanceSynced = true;
				const localAppearance = loadLocalAppearance();
				api
					.getAppearanceSettings()
					.then((remote) =>
						remote.configured
							? remote
							: api.saveAppearanceSettings(localAppearance)
					)
					.then((appearance) => applyAppearanceSettings(appearance))
					.catch(() => {
						// The pre-rendered local appearance remains active offline.
					});
			}
			refreshNotesSafely();
			refreshPendingCount();
			refreshOverseerStatus();
			// Deliver any reminders that came due while the app was closed.
			void pollAlerts();
			// Re-register this device's push subscription: the server prunes dead
			// endpoints, and only this heal tells it about the device again.
			void syncPush();
			// Morning briefing: greeting (server-deduped once/day) + today/week
			// data. Shows unless already dismissed for the local day.
			briefingDay = localToday();
			void refreshMorning();
		}
	});

	// Fill the tab bar's destinations while the user is still on whatever they
	// opened — the media hub and OMNIA both used to fetch on mount, so switching
	// tabs meant watching a loading screen. Runs at idle and skips itself on Data
	// Saver; see preload.svelte.ts.
	$effect(() => {
		if (authState !== 'ok') return;
		return startPreload();
	});

	// One live connection for the whole app, re-broadcast as a DOM event so the
	// open note (and anything else that cares) can react without each route
	// opening its own stream.
	$effect(() => {
		liveGeneration;
		if (authState !== 'ok') return;
		if (isDesktop) {
			// The sync engine owns the live connection on desktop: it applies
			// remote changes to the local cache first, then re-broadcasts the
			// same 'exscalia:live' events the web path dispatches directly.
			let stop: (() => void) | undefined;
			let cancelled = false;
			void import('$lib/sync/engine').then((engine) => {
				if (cancelled) return;
				stop = engine.startSync({
					onAuthExpired: () => window.dispatchEvent(new Event('exscalia:auth-expired'))
				});
			});
			return () => {
				cancelled = true;
				stop?.();
			};
		}
		let wasOffline = false;
		return connectLive(
			(event) => {
				window.dispatchEvent(new CustomEvent('exscalia:live', { detail: event }));
				// `paper` never touches the note tree — the /paper view listens
				// for those itself.
				if (event.type === 'paper') return;
				// The echo of our own save of a note the tree already lists
				// changes nothing it shows but the date: stamp that in place
				// (keeps "Date modified" sorting right) rather than refetch the
				// tree — two vault walks — on every autosave. A frontmatter edit
				// can also flip `pinned`/`board`; those wait for the next
				// structural refresh. Pin/unpin refreshes explicitly, and OMNIA
				// writes carry no origin, so both still refetch below.
				if (event.type === 'note' && event.origin === CLIENT_ID) {
					const item = vault.notes.find((note) => note.path === event.path);
					if (item) {
						// The server's own stamp format, so the next refetch compares equal.
						item.modified = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
						return;
					}
				}
				if (event.type === 'note_moved') followRemoteMove(event);
				// A note appearing, moving, or vanishing elsewhere changes the
				// sidebar too. `note` also fires for plain saves, so coalesce a
				// burst (an OMNIA bulk edit, a folder rename) into one refetch.
				scheduleSidebarRefresh();
			},
			{
				// The sidebar's sync dot follows the stream on the web too, so
				// a dead connection is visible instead of silently stale.
				onState: (state) => {
					syncState.mode = state;
					if (state === 'live') {
						if (wasOffline) resyncAfterGap();
						else if (!vault.loaded && notesLoading === 0) refreshNotesSafely();
					}
					wasOffline = state === 'offline' || (wasOffline && state !== 'live');
				},
				// A revoked session stops the reconnect loop instead of
				// hammering it every 30s — same contract the desktop path has.
				onAuthExpired: () => window.dispatchEvent(new Event('exscalia:auth-expired'))
			}
		);
	});

	// The note tree feeds the sidebar, the palette and home. A failed fetch was
	// an unhandled rejection that left it blank until a manual reload; retry
	// once shortly, and the live stream's resync covers a longer outage.
	let notesLoading = 0;
	function refreshNotesSafely() {
		notesLoading++;
		refreshNotes()
			.catch(() => new Promise((resolve) => setTimeout(resolve, 3000)).then(refreshNotes))
			.catch(() => {
				// Still unreachable; the next reconnect or change tries again.
			})
			.finally(() => notesLoading--);
	}

	// The stream keeps no history: whatever was published while it was down
	// (asleep, backgrounded, a deploy restart) is gone. Coming back, re-read
	// what it would have kept fresh — the tree and the tray here, tags through
	// data-changed, and a stand-in `note` event for the open note (its guarded
	// pull), the task boards and the cached calendar months.
	function resyncAfterGap() {
		refreshNotesSafely();
		void refreshPendingCount();
		window.dispatchEvent(new CustomEvent('exscalia:data-changed'));
		const openNote = page.url.pathname.startsWith('/note/') ? (page.params.path ?? '') : '';
		const pulse: LiveNoteEvent = { type: 'note', path: openNote, origin: null };
		window.dispatchEvent(new CustomEvent('exscalia:live', { detail: pulse }));
	}

	/** A note moved on another device: this browser's draft, OMNIA thread
	 * pointer, tabs and recents for it follow. The client that moved it has
	 * already done its own. */
	function followRemoteMove(event: LiveNoteEvent) {
		if (event.origin === CLIENT_ID || !event.new_path) return;
		void afterPathsMoved([[event.path, event.new_path]]);
	}

	let sidebarRefresh: ReturnType<typeof setTimeout> | null = null;
	function scheduleSidebarRefresh() {
		if (sidebarRefresh) clearTimeout(sidebarRefresh);
		sidebarRefresh = setTimeout(() => {
			sidebarRefresh = null;
			refreshNotesSafely();
		}, 500);
	}

	// Sidebar refresh on remote removals, desktop and web alike.
	$effect(() => {
		if (authState !== 'ok' || !isDesktop) return;
		const onLive = (e: Event) => {
			const detail = (e as CustomEvent).detail as LiveNoteEvent | undefined;
			if (detail?.type === 'note_moved') followRemoteMove(detail);
			if (
				detail?.type === 'note' ||
				detail?.type === 'note_removed' ||
				detail?.type === 'note_moved'
			)
				scheduleSidebarRefresh();
		};
		// The startup list is read from the cache before the engine connects,
		// and the manifest pull applies changed notes without per-note events.
		// Reaching 'live' means cache and vault agree, so relist once then.
		const onSyncMode = (e: Event) => {
			if ((e as CustomEvent).detail === 'live') {
				void refreshNotes();
				void refreshTags();
			}
		};
		const onConflict = (e: Event) => {
			const { path, conflictPath } = (e as CustomEvent).detail as {
				path: string;
				conflictPath: string;
			};
			const name = (path.split('/').pop() ?? path).replace(/\.md$/, '');
			notify(`Another device changed “${name}” while you were offline — your version was kept as a copy.`, {
				actionLabel: 'Open copy',
				action: () => goto(`/note/${conflictPath.split('/').map(encodeURIComponent).join('/')}`)
			});
		};
		window.addEventListener('exscalia:live', onLive);
		window.addEventListener('exscalia:sync-mode', onSyncMode);
		window.addEventListener('exscalia:sync-conflict', onConflict);
		return () => {
			window.removeEventListener('exscalia:live', onLive);
			window.removeEventListener('exscalia:sync-mode', onSyncMode);
			window.removeEventListener('exscalia:sync-conflict', onConflict);
		};
	});

	$effect(() => {
		if (authState !== 'ok') return;
		// Skip the poll while the tab is hidden — a backgrounded tab was hitting
		// the alerts endpoint every minute indefinitely — and catch up as soon
		// as it comes back, so a reminder that fell due meanwhile still lands.
		// A long-open session that crosses midnight should surface the new day's
		// briefing without a reload — refetch when the local date has rolled.
		const rollover = () => {
			const today = localToday();
			if (today !== briefingDay) {
				briefingDay = today;
				void refreshMorning();
			}
		};
		const timer = setInterval(() => {
			if (document.visibilityState === 'visible') {
				void pollAlerts();
				rollover();
			}
		}, 60_000);
		const onVisible = () => {
			if (document.visibilityState === 'visible') {
				void pollAlerts();
				rollover();
			}
		};
		document.addEventListener('visibilitychange', onVisible);
		return () => {
			clearInterval(timer);
			document.removeEventListener('visibilitychange', onVisible);
		};
	});

	// Tell the user when a newer build has shipped. Without this the running
	// page keeps serving whatever JS it loaded at open — the reason UI changes
	// "don't show up" until the app is force-relaunched. `updated.check()` polls
	// the deployed version manifest; `updated.current` flips true on a mismatch.
	// Web/PWA only — the desktop app updates through its own Tauri updater.
	$effect(() => {
		if (authState !== 'ok' || isDesktop) return;
		void updated.check();
		const timer = setInterval(() => {
			if (document.visibilityState === 'visible') void updated.check();
		}, 60_000);
		const onVisible = () => {
			if (document.visibilityState === 'visible') void updated.check();
		};
		document.addEventListener('visibilitychange', onVisible);
		return () => {
			clearInterval(timer);
			document.removeEventListener('visibilitychange', onVisible);
		};
	});

	$effect(() => {
		page.url.pathname;
		mobileNavOpen = false;
	});

	// Tell OMNIA which view the user is on, so off-note "what's on here?" resolves.
	$effect(() => {
		omniaContext.view = routeTitle(page.url.pathname);
	});

	function closeMobileNav() {
		mobileNavOpen = false;
	}

	async function submitLogin(e: Event) {
		e.preventDefault();
		loginError = null;
		try {
			if (isDesktop) {
				await setServer(serverInput);
				// The cache is per server (sync/db.ts): switch files before the
				// engine that starts on auth-ok replays anything. A cache that
				// cannot open does not block sign-in: the app runs network-only,
				// as +layout.ts does at startup (openCacheFor puts the network
				// methods back over the wrappers).
				const offlineApi = await import('$lib/sync/offline-api');
				try {
					await offlineApi.openCacheFor(apiBase());
				} catch (err) {
					console.error(err);
					syncState.lastError = (err as Error)?.message ?? String(err);
				}
				const result = await api.login(username, password, {
					createToken: true,
					client: 'EXSCALIA Desktop'
				});
				if (!result.token) throw new Error('Server did not issue a token — update it first.');
				await setToken(result.token);
			} else {
				await api.login(username, password);
			}
			password = '';
			if (reauth) {
				reauth = false;
				liveGeneration++;
				// Catch up on what changed while signed out; the note page's own
				// save backoff retries the pending save by itself.
				refreshNotesSafely();
				refreshPendingCount();
				void pollAlerts();
			}
			authState = 'ok';
		} catch (err) {
			loginError = (err as Error).message;
		}
	}

	// Close-time gate: don't let AI changes silently persist. Leaving with a
	// non-empty tray prompts the browser's native "are you sure" dialog.
	$effect(() => {
		const guard = (e: BeforeUnloadEvent) => {
			if (vault.pendingCount > 0) {
				e.preventDefault();
				e.returnValue = '';
			}
		};
		window.addEventListener('beforeunload', guard);
		return () => window.removeEventListener('beforeunload', guard);
	});
</script>

<svelte:head>
	<title>EXSCALIA</title>
	<link rel="icon" href={favicon} />
</svelte:head>

<svelte:window
	onkeydown={(e) => {
		// Escape dismisses the desktop briefing banner (the mobile sheet owns its
		// own Escape, and so does the drawer). Only an Escape nothing else
		// claimed: an open modal (palette, dialog, sheet), a menu, a field or
		// the editor use it for themselves, and the dismissal lasts all day.
		if (
			e.key === 'Escape' &&
			morning.visible &&
			!isMobile &&
			!modalOpen() &&
			!(e.target as Element | null)?.closest?.(
				'.cm-editor, input, textarea, [contenteditable="true"]'
			)
		) {
			// Window listeners added after this one (the sidebar's context
			// menu, the media popover) have not seen the key yet; decide once
			// the whole dispatch is over. Not a microtask: those run between
			// listeners.
			setTimeout(() => {
				if (!e.defaultPrevented) dismissMorning();
			});
		}
	}}
/>

{#if authState === 'unknown'}
	<div class="login-wrap" aria-hidden="true"></div>
{:else if authState === 'login' || reauth}
	<div class="login-wrap">
		<form class="login" onsubmit={submitLogin} in:fly={{ y: 8, duration: 220, easing: cubicOut }}>
			<h1>EXSCALIA</h1>
			<!-- Re-auth keeps the mounted shell, which belongs to this server. -->
			{#if isDesktop && !reauth}
				<input
					type="url"
					placeholder="Server"
					bind:value={serverInput}
					autocapitalize="none"
					spellcheck="false"
					required
				/>
			{/if}
			<input
				type="text"
				placeholder="Username"
				bind:value={username}
				autocomplete="username"
				autocapitalize="none"
				spellcheck="false"
				required
			/>
			<input
				type="password"
				placeholder="Password"
				bind:value={password}
				autocomplete="current-password"
				required
			/>
			<button class="primary" type="submit">Unlock</button>
			{#if loginError}<p class="login-error" role="alert">{loginError}</p>{/if}
		</form>
	</div>
{/if}
{#if authState === 'ok'}
	{#if !isDesktop && updated.current}
		<div class="update-banner" role="status" transition:fly={{ y: -10, duration: 200, easing: cubicOut }}>
			<span>A new version of EXSCALIA is ready.</span>
			<button type="button" onclick={() => location.reload()}>Reload</button>
		</div>
	{/if}
	{#if navSlow}
		<div class="nav-progress" aria-hidden="true" transition:fade={{ duration: 120 }}></div>
	{/if}
	<div
		class="shell"
		class:nav-open={mobileNavOpen}
		class:immersive
		class:rail-only={sidebarHidden && !immersive}
		inert={reauth}
		in:fade={{ duration: 160 }}
	>
		<div class="side-holder" class:hidden={sidebarHidden || immersive}>
			<Sidebar mobileOpen={mobileNavOpen} onClose={closeMobileNav} onCollapse={toggleSidebar} />
		</div>
		{#if sidebarHidden && !immersive}
			<div class="side-rail" in:fade={{ duration: 140 }}>
				<button type="button" title="Show sidebar" aria-label="Show sidebar" onclick={toggleSidebar}>
					☰
				</button>
			</div>
		{/if}
		{#if mobileNavOpen}
			<button
				class="mobile-scrim"
				type="button"
				aria-label="Close navigation"
				onclick={closeMobileNav}
				transition:fade={{ duration: 150 }}
			></button>
		{/if}
		<main class:media-under={mediaUnder} class:recipes-under={recipesUnder}>
			{#if !immersive}
				<!-- iOS 26's Liquid Glass status bar scans for a FIXED element at
				     the top edge and tints from its background-color; without one
				     it blur-samples raw page pixels, smearing the header title
				     into the status zone. This cover is that element. -->
				<div class="statusbar-cover" aria-hidden="true"></div>
			{/if}
			<div class="mobile-bar" class:hidden={immersive}>
				<button
					class="menu-button"
					type="button"
					aria-label="Open navigation"
					aria-expanded={mobileNavOpen}
					onclick={() => (mobileNavOpen = true)}
				>
					<span></span><span></span><span></span>
				</button>
				<button
					class="mobile-heading"
					type="button"
					aria-label="Go home"
					onclick={() => goto('/')}
				>
					<strong>{mobileTitle}</strong>
				</button>
				<button
					class="mobile-command"
					type="button"
					aria-label="Open command palette"
					title="Commands"
					onclick={openPalette}
				>
					<Glyph name="search" />
				</button>
			</div>
			{#if !immersive}
				<TabStrip />
			{/if}
			{#if morning.visible && !isMobile && MorningBriefing}
				<!-- Desktop: the briefing expands the old greeting banner in place,
				     top-anchored under the strip, no scrim. -->
				<div
					class="morning-card"
					role="status"
					transition:fly={{ y: -8, duration: 190, easing: cubicOut }}
				>
					<MorningBriefing />
				</div>
			{/if}
			{#key routeKey}
				<!-- In-only fade: an out transition would keep the old page in flow
				     alongside the new one, shifting the whole layout mid-switch. -->
				<div class="route-view" in:routeIntro>
					{@render children()}
				</div>
			{/key}
			<!-- Outside the {#key}: remounting the bar on every navigation would
			     rebuild it exactly when it is being tapped. In flow after the
			     route view, so the route shrinks instead of hiding behind it.
			     Study mode owns the whole window and gets no bar. -->
			{#if !immersive}
				<MobileTabBar />
			{/if}
		</main>
		<PendingBox />
		<MediaAddDialog />
		{#if morning.visible && isMobile && MorningBriefing}
			<!-- Mobile: a native bottom sheet instead of the top banner. It supplies
			     the date header + close, so the body renders embedded. -->
			<BottomSheet
				title={new Date().toLocaleDateString(undefined, {
					weekday: 'long',
					month: 'long',
					day: 'numeric'
				})}
				label="Today's briefing"
				onclose={dismissMorning}
			>
				<MorningBriefing embedded />
			</BottomSheet>
		{/if}
	</div>
{/if}

<UIHost />

<!-- The Texture setting's overlay (app.css). Paper is exempt: handwriting is
     shown as written. -->
{#if !page.url.pathname.startsWith('/paper')}
	<div class="app-texture" aria-hidden="true">
		<div class="app-texture-scan"></div>
		<div class="app-texture-grain"></div>
	</div>
{/if}

<style>
	.shell {
		display: flex;
		height: 100vh;
	}
	/* Study mode takes the window whole — no tab bar renders, so nothing
	   inside should still be reserving room for one. */
	.shell.immersive {
		--tabbar-h: 0px;
	}
	.update-banner {
		position: fixed;
		top: 0;
		left: 0;
		right: 0;
		z-index: 200;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 0.9rem;
		padding: 0.55rem 1rem;
		font-size: 0.85rem;
		color: var(--text);
		background: var(--accent-soft);
		border-bottom: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
		box-shadow: 0 2px 12px color-mix(in srgb, var(--accent) 12%, transparent);
	}
	.update-banner button {
		padding: 0.3rem 0.85rem;
		border: 1px solid var(--accent);
		border-radius: 6px;
		background: var(--accent);
		color: var(--accent-contrast, #fff);
		font-size: 0.82rem;
		font-weight: 600;
		cursor: pointer;
		transition: background-color var(--motion-base) var(--ease-out);
	}
	.update-banner button:hover {
		background: color-mix(in srgb, var(--accent) 85%, #000);
	}
	.nav-progress {
		position: fixed;
		top: 0;
		left: 0;
		right: 0;
		height: 2px;
		z-index: 100;
		overflow: hidden;
		pointer-events: none;
	}
	.nav-progress::after {
		content: '';
		position: absolute;
		inset: 0;
		width: 35%;
		border-radius: 2px;
		background: var(--accent);
		animation: nav-sweep 0.9s var(--ease-out) infinite;
	}
	@keyframes nav-sweep {
		from {
			transform: translateX(-100%);
		}
		to {
			transform: translateX(390%);
		}
	}
	.side-holder {
		width: var(--sidebar-w);
		flex-shrink: 0;
		overflow: hidden;
		transition: width 240ms var(--ease-out);
	}
	.side-holder.hidden {
		width: 0;
	}
	.mobile-bar.hidden {
		display: none;
	}
	.side-rail {
		width: 32px;
		flex-shrink: 0;
		background: var(--bg-panel);
		border-right: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		align-items: center;
		padding-top: 8px;
	}
	/* With the sidebar collapsed the traffic lights land on the rail and the
	   start of the tab strip, so both step out of their way. */
	:global(html[data-desktop='mac']) .shell.rail-only .side-rail {
		padding-top: 44px;
	}
	.side-rail button {
		width: 24px;
		height: 26px;
		padding: 0;
		border: none;
		color: var(--text-muted);
	}
	.side-rail button:not(:disabled):hover {
		color: var(--accent);
		background: var(--bg-hover);
	}
	.mobile-bar,
	.mobile-scrim {
		display: none;
	}
	main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		height: 100%;
	}
	/* Media mode: the strip simply wears the media pages' black, one flat
	   surface with the pane and the sidebar. */
	main.media-under :global(.strip),
	main.recipes-under :global(.strip) {
		background: #000000;
		border-bottom-color: color-mix(in srgb, #ffffff 9%, transparent);
	}
	.route-view {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.login-wrap {
		position: fixed;
		inset: 0;
		z-index: 1000;
		min-height: 100vh;
		min-height: 100dvh;
		display: flex;
		align-items: center;
		justify-content: center;
		background: #000;
		color: #fff;
	}
	.login {
		display: flex;
		flex-direction: column;
		gap: 10px;
		width: 260px;
		text-align: center;
	}
	.login h1 {
		color: #fff;
		font-size: 20px;
		margin: 0 0 6px;
	}
	.login input {
		color: #fff;
		background: #000;
		border-color: #333;
		caret-color: #fff;
	}
	.login input::placeholder {
		color: #777;
	}
	.login input:hover {
		border-color: #666;
	}
	.login input:focus {
		border-color: #fff;
		box-shadow: 0 0 0 1px #fff;
	}
	.login input:-webkit-autofill,
	.login input:-webkit-autofill:hover,
	.login input:-webkit-autofill:focus {
		-webkit-text-fill-color: #fff;
		box-shadow: 0 0 0 1000px #000 inset;
		caret-color: #fff;
	}
	.login button.primary {
		color: #fff;
		background: #000;
		border-color: #fff;
		box-shadow: none;
	}
	.login button.primary:hover {
		color: #000;
		background: #fff;
		filter: none;
		box-shadow: none;
	}
	.login-error {
		color: #ff6b6b;
		font-size: 13px;
		margin: 0;
	}
	/* Desktop briefing banner: the expanded greeting card, top-anchored under
	   the strip. Holds the MorningBriefing body; no scrim. */
	.morning-card {
		margin: 12px 16px 0;
		padding: 12px 14px;
		max-width: 640px;
		max-height: min(70vh, 640px);
		overflow: hidden;
		display: flex;
		flex-direction: column;
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: 12px;
		flex-shrink: 0;
		box-shadow: var(--lift-shadow);
	}

	@media (max-width: 760px) {
		.shell {
			height: 100dvh;
		}
		/* Mobile uses the fixed drawer — the collapse wrapper must not trap it. */
		.side-holder,
		.side-holder.hidden {
			display: contents;
		}
		.side-rail {
			display: none;
		}
		main {
			height: 100dvh;
			width: 100%;
			/* The tab bar is fixed to the true viewport bottom; routes clear it
			   through this padding instead of the bar's former in-flow height. */
			padding-bottom: var(--tabbar-h);
			box-sizing: border-box;
		}
		/* On media routes the standard media look owns the WHOLE screen: the
		   top bar and tab bar are layout chrome outside .media-shell, and in
		   the app theme they clashed against the black pane. Same tokens as
		   .media-shell (keep in step); --accent still follows the app theme. */
		main.media-under {
			--bg: #000000;
			--bg-panel: #17171a;
			--border: #2a2a2e;
			--text: #ffffff;
			/* The media look owns the secondary colour on the CHROME too: the top
			   bar wordmark and the tab-bar labels read white regardless of theme.
			   .media-shell re-declares its own muted grey, so the media pages'
			   internal hierarchy (stat labels, bylines) is unaffected. */
			--text-muted: #ffffff;
			--accent-soft: color-mix(in srgb, var(--accent) 16%, transparent);
			--bg-hover: color-mix(in srgb, #ffffff 7%, transparent);
			background: var(--bg);
			color: var(--text);
		}
		/* Recipes, likewise, in the recipe app's ink (keep in step with
		   :root[data-recipes-look] in lib/recipes/recipes.css). */
		main.recipes-under {
			--bg: #000000;
			--bg-panel: #17171a;
			--border: #2a2a2e;
			--text: #e4e7f5;
			--text-muted: #a3a8c4;
			--accent-soft: color-mix(in srgb, var(--accent) 16%, transparent);
			--bg-hover: color-mix(in srgb, #ffffff 7%, transparent);
			background: var(--bg);
			color: var(--text);
		}
		.statusbar-cover {
			position: fixed;
			top: 0;
			left: 0;
			right: 0;
			height: env(safe-area-inset-top, 0px);
			background: var(--bg);
			z-index: 36;
		}
		.mobile-bar {
			display: grid;
			grid-template-columns: 44px minmax(0, 1fr) 44px;
			align-items: center;
			height: calc(52px + env(safe-area-inset-top));
			padding: env(safe-area-inset-top) 6px 0;
			border-bottom: 1px solid var(--border);
			/* Opaque, flat header. The old translucent + backdrop-filter frosted
			   look had nothing to frost — the bar is a static in-flow header, so
			   no content ever scrolls behind it — and on iOS the blur layer
			   re-sampled the bar's own small "EXSCALIA" label into a smear. */
			background: var(--bg);
			flex-shrink: 0;
			z-index: 35;
		}
		/* Three spans standing in for an icon: proportioned like one drawn at
		   18px wide with a 2px stroke and a 5px gap, so the glyph on the other
		   end of the bar and this one carry the same weight. The 44px box is
		   the touch target; the ~18x12 stack is centred inside it. */
		.menu-button {
			width: 44px;
			height: 44px;
			padding: 0;
			display: flex;
			flex-direction: column;
			align-items: center;
			justify-content: center;
			gap: 3px;
			color: var(--text-muted);
		}
		.menu-button span {
			display: block;
			width: 18px;
			height: 2px;
			border-radius: 2px;
			background: currentColor;
		}
		.menu-button:active {
			color: var(--text);
		}
		.mobile-heading {
			min-width: 0;
			text-align: center;
			line-height: 1.2;
			/* It's a button now (tap = home), but it must keep reading as the
			   heading it always was. */
			border: 0;
			background: none;
			padding: 0;
			font: inherit;
			color: inherit;
		}
		/* One line, one job: the route you are on. The stacked "EXSCALIA"
		   microbrand above it spent the bar's whole vertical budget saying
		   which app this is — something the home screen and the tab bar
		   already answer. */
		.mobile-heading strong {
			display: block;
			overflow: hidden;
			font-size: 16px;
			font-weight: 600;
			text-overflow: ellipsis;
			white-space: nowrap;
		}
		.mobile-command {
			display: flex;
			align-items: center;
			justify-content: center;
			width: 44px;
			height: 44px;
			padding: 0;
			border: 0;
			color: var(--text-muted);
		}
		.mobile-command:active {
			color: var(--text);
		}
		.mobile-command :global(svg) {
			display: block;
			width: var(--m-icon-bar);
			height: var(--m-icon-bar);
		}
		.mobile-scrim {
			display: block;
			position: fixed;
			inset: 0;
			z-index: var(--z-scrim);
			width: 100%;
			height: 100%;
			min-height: 0;
			padding: 0;
			border: 0;
			border-radius: 0;
			background: rgb(0 0 0 / 42%);
			transform: none !important;
		}
		.route-view {
			overflow: hidden;
		}
		.login-wrap {
			padding: 20px;
		}
		.login {
			width: min(100%, 320px);
		}
	}
</style>
