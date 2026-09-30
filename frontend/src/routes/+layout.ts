import { error } from '@sveltejs/kit';
import { apiBase, initRuntime, isDesktop } from '$lib/runtime';
import { syncState } from '$lib/stores.svelte';

export const ssr = false;
export const prerender = false;

/** Desktop needs its server URL + keychain token loaded — and the offline
 * cache opened and installed over the api — before any API call; on the web
 * this resolves immediately. */
export async function load() {
	await step('settings store', initRuntime);
	if (isDesktop) {
		const [db, offlineApi] = await Promise.all([
			import('$lib/sync/db'),
			import('$lib/sync/offline-api')
		]);
		try {
			await db.openSyncDb(apiBase());
			offlineApi.installOfflineApi();
		} catch (err) {
			// No usable cache, e.g. one written by a newer build after a
			// downgrade: run network-only, like the web, and leave the file
			// alone. Settings → Sync shows why.
			console.error(err);
			syncState.lastError = (err as Error)?.message ?? String(err);
		}
	}
}

/** Run one init step, rethrowing a failure as a 500 that names the step, so the
 * error page says what broke instead of 'Internal Error'. Nothing is swallowed:
 * the app cannot run without it. An HttpError skips handleError, so the
 * original (with its stack) is logged here. */
async function step(name: string, run: () => unknown): Promise<void> {
	try {
		await run();
	} catch (e) {
		console.error(e);
		error(500, `${name}: ${e instanceof Error ? e.message : String(e)}`);
	}
}
