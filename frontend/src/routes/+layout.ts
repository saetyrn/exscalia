import { initRuntime, isDesktop } from '$lib/runtime';

export const ssr = false;
export const prerender = false;

/** Desktop needs its server URL + keychain token loaded — and the offline
 * cache opened and installed over the api — before any API call; on the web
 * this resolves immediately. */
export async function load() {
	await initRuntime();
	if (isDesktop) {
		const [db, offlineApi] = await Promise.all([
			import('$lib/sync/db'),
			import('$lib/sync/offline-api')
		]);
		await db.openSyncDb();
		offlineApi.installOfflineApi();
	}
}
