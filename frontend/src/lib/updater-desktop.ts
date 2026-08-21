// Desktop only. One quiet update check shortly after startup; if a newer
// build is published, a notification offers to install it. No background
// polling — the app restarts often enough that startup checks suffice.

import { notify } from './ui.svelte';

export async function checkForUpdates(): Promise<void> {
	try {
		const { check } = await import('@tauri-apps/plugin-updater');
		const update = await check();
		if (!update) return;
		notify(`Update ${update.version} is available.`, {
			actionLabel: 'Install',
			action: () => {
				void (async () => {
					try {
						await update.downloadAndInstall();
						const { relaunch } = await import('@tauri-apps/plugin-process');
						await relaunch();
					} catch (err) {
						notify(`Update failed: ${(err as Error).message}`, { tone: 'error' });
					}
				})();
			}
		});
	} catch {
		// Offline, endpoint missing, or signature mismatch — never bother the
		// user about a background check.
	}
}
