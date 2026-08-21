/// <reference types="@sveltejs/kit" />
/// <reference lib="webworker" />
// App-shell cache for the web app. Two rules, nothing clever:
//
//   * /api is never touched — API responses are live vault state.
//   * The shell (built JS/CSS, static assets, index.html) is served
//     cache-first, so a reload on a train still opens the app, which then
//     reports "offline" through its own UI instead of a browser error page.
//
// The desktop app never registers this: its sync engine owns offline.

const sw = self as unknown as ServiceWorkerGlobalScope;

import { build, files, version } from '$service-worker';

const CACHE = `shell-${version}`;
// `build`/`files` don't include the adapter's fallback page, and that is the
// one thing an offline navigation needs — cache it explicitly.
const SHELL = [...build, ...files, '/index.html'];

// True when this worker replaces an older one (a deploy), false on the very
// first install. Read at install time: only an UPDATE leaves stale windows
// behind, so only an update may reload clients below.
let replacesOldWorker = false;

sw.addEventListener('install', (event) => {
	replacesOldWorker = !!sw.registration.active;
	event.waitUntil(
		caches
			.open(CACHE)
			.then((cache) => cache.addAll(SHELL))
			.then(() => sw.skipWaiting())
	);
});

sw.addEventListener('activate', (event) => {
	event.waitUntil(
		caches
			.keys()
			.then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
			.then(() => sw.clients.claim())
			// Long-lived windows keep running the bundle they loaded — pages
			// older than the layout's controllerchange listener can never heal
			// themselves. A freshly activated worker may navigate the windows it
			// now controls, so refresh them from here: each open client reloads
			// exactly once per new version and comes back on the current shell.
			// First installs skip this — a page that just loaded the current
			// bundle has nothing stale, and reloading it out from under scripts
			// (or automation) causes far more harm than the flicker suggests.
			.then(() => (replacesOldWorker ? sw.clients.matchAll({ type: 'window' }) : []))
			.then((clients) =>
				Promise.all(
					clients.map((client) =>
						'navigate' in client ? client.navigate(client.url).catch(() => null) : null
					)
				)
			)
	);
});

sw.addEventListener('fetch', (event) => {
	const { request } = event;
	if (request.method !== 'GET') return;
	const url = new URL(request.url);
	if (url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
	// Never intercept the version manifest: the update check must always see the
	// freshly deployed version, or the app can't tell it's running stale code.
	if (url.pathname.endsWith('/version.json')) return;

	// Navigations: network first (a deploy shows up on the next load), cached
	// index.html when the network is gone.
	if (request.mode === 'navigate') {
		event.respondWith(
			fetch(request)
				.then((response) => {
					const copy = response.clone();
					void caches.open(CACHE).then((cache) => cache.put('/index.html', copy));
					return response;
				})
				.catch(() => caches.match('/index.html').then((hit) => hit ?? Response.error()))
		);
		return;
	}

	// The shell: cache first — build assets are content-hashed, so a hit can
	// never be stale.
	event.respondWith(
		caches.match(request).then(
			(hit) =>
				hit ??
				fetch(request).then((response) => {
					if (response.ok && SHELL.includes(url.pathname)) {
						const copy = response.clone();
						void caches.open(CACHE).then((cache) => cache.put(request, copy));
					}
					return response;
				})
		)
	);
});

// OMNIA reminders arrive here even when the app is closed. The backend sends a
// JSON payload of { title, body, url, tag } (see backend/app/push_service.py).
sw.addEventListener('push', (event) => {
	const data = (() => {
		try {
			return (event.data?.json() ?? {}) as {
				title?: string;
				body?: string;
				url?: string;
				tag?: string;
			};
		} catch {
			return {};
		}
	})();
	event.waitUntil(
		sw.registration.showNotification(data.title ?? 'EXSCALIA', {
			body: data.body ?? '',
			icon: '/icon-192.png',
			badge: '/icon-192.png',
			tag: data.tag,
			data: { url: data.url ?? '/' }
		})
	);
});

// Tapping a notification focuses an open window (deep-linked to the payload's
// url) or opens a new one.
sw.addEventListener('notificationclick', (event) => {
	event.notification.close();
	const target = (event.notification.data as { url?: string } | null)?.url ?? '/';
	event.waitUntil(
		sw.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
			const client = clients[0];
			if (client) {
				void client.navigate(target);
				return client.focus();
			}
			return sw.clients.openWindow(target);
		})
	);
});
