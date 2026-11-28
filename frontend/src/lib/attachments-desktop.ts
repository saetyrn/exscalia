// Desktop only. Rendered notes and chat embed images as relative
// `/api/attachments/…` URLs, and the Paper viewer embeds page renders as
// `/api/paper/blob/…`; inside the tauri://localhost origin those resolve
// nowhere, and an <img> tag cannot carry the bearer header anyway.
// Watch the document for such images and swap in authenticated blob URLs.
// The match is every `/api/` image rather than a family whitelist, so a new
// binary route is covered the day it exists.

import { apiFetch, LONG_TIMEOUT_MS } from './api';

const MAX_CACHED = 64;
const cache = new Map<string, Promise<string>>(); // path → object URL

function objectUrlFor(path: string): Promise<string> {
	let entry = cache.get(path);
	if (!entry) {
		// The deadline covers the body: a photo or a page render on a slow
		// link needs the download allowance, not the 30 s of a JSON call.
		entry = apiFetch(path, { timeoutMs: LONG_TIMEOUT_MS }).then(async (res) => {
			if (!res.ok) throw new Error(`attachment ${res.status}`);
			return URL.createObjectURL(await res.blob());
		});
		entry.catch(() => cache.delete(path)); // allow retry after failures
		cache.set(path, entry);
		if (cache.size > MAX_CACHED) {
			const [oldestPath, oldest] = cache.entries().next().value!;
			cache.delete(oldestPath);
			oldest.then((url) => URL.revokeObjectURL(url)).catch(() => {});
		}
	}
	return entry;
}

function hydrate(img: HTMLImageElement) {
	const src = img.getAttribute('src');
	if (!src || !src.startsWith('/api/')) return;
	img.removeAttribute('src'); // stop the webview from trying the bad URL
	objectUrlFor(src)
		.then((url) => {
			img.src = url;
		})
		.catch(() => {
			img.setAttribute('alt', img.getAttribute('alt') || 'attachment unavailable');
		});
}

function sweep(root: ParentNode) {
	root.querySelectorAll?.('img[src^="/api/"]').forEach((el) => hydrate(el as HTMLImageElement));
}

/** Start watching. Returns a stop function (unused in practice — the observer
 * lives as long as the app window). */
export function watchAttachmentImages(): () => void {
	sweep(document);
	const observer = new MutationObserver((mutations) => {
		for (const m of mutations) {
			if (m.type === 'attributes' && m.target instanceof HTMLImageElement) {
				hydrate(m.target);
				continue;
			}
			for (const node of m.addedNodes) {
				if (node instanceof HTMLImageElement) hydrate(node);
				else if (node instanceof Element) sweep(node);
			}
		}
	});
	observer.observe(document.documentElement, {
		childList: true,
		subtree: true,
		attributes: true,
		attributeFilter: ['src']
	});
	return () => observer.disconnect();
}
