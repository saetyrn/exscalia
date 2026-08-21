// Where am I running, and where is the server? On the web these answers are
// baked in (same origin, cookie auth) and this module compiles down to
// constants. Inside the Tauri desktop app the server is a user-configured
// remote and auth is a bearer token held in the OS keychain, mirrored here
// so request paths stay synchronous.

export const isDesktop =
	typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

export const DEFAULT_SERVER = 'https://notes.exscalia.art';

/** Which desktop shell this is, so the top row can leave space for the window
 * controls: macOS floats its traffic lights over the left, Windows has no
 * decorations at all and we draw the buttons ourselves on the right. */
export type DesktopPlatform = 'mac' | 'windows' | 'other';

export const desktopPlatform: DesktopPlatform | null = !isDesktop
	? null
	: typeof navigator !== 'undefined' && navigator.userAgent.includes('Mac')
		? 'mac'
		: typeof navigator !== 'undefined' && navigator.userAgent.includes('Windows')
			? 'windows'
			: 'other';

let serverUrl = '';
let token: string | null = null;

type TauriStore = {
	get<T>(key: string): Promise<T | null | undefined>;
	set(key: string, value: unknown): Promise<void>;
	save(): Promise<void>;
};

let store: TauriStore | null = null;
let invoke: (<T>(cmd: string, args?: Record<string, unknown>) => Promise<T>) | null = null;

/** Load persisted server URL + keychain token before the app renders.
 * No-op on the web. */
export async function initRuntime(): Promise<void> {
	if (!isDesktop) return;
	// Set before the first render so the window-chrome insets are in place on
	// the very first paint rather than shifting the top row a frame later.
	document.documentElement.dataset.desktop = desktopPlatform ?? 'other';
	const [core, storePlugin] = await Promise.all([
		import('@tauri-apps/api/core'),
		import('@tauri-apps/plugin-store')
	]);
	invoke = core.invoke;
	store = await storePlugin.load('settings.json', { autoSave: false });
	serverUrl = ((await store.get<string>('serverUrl')) ?? DEFAULT_SERVER).replace(/\/+$/, '');
	try {
		token = await invoke<string | null>('secret_get');
	} catch {
		token = null; // Keychain unavailable — the login screen will ask again.
	}
}

/** Origin to prefix API paths with: '' on the web (same origin). */
export function apiBase(): string {
	return isDesktop ? serverUrl : '';
}

export async function setServer(url: string): Promise<void> {
	serverUrl = url.replace(/\/+$/, '');
	if (store) {
		await store.set('serverUrl', serverUrl);
		await store.save();
	}
}

export function getToken(): string | null {
	return token;
}

export async function setToken(value: string | null): Promise<void> {
	token = value;
	if (!invoke) return;
	try {
		if (value === null) await invoke('secret_delete');
		else await invoke('secret_set', { value });
	} catch {
		// Keychain write failed; the in-memory token still works this session.
	}
}

/** Extra headers every API request needs: none on the web (cookies), the
 * bearer token on desktop. */
export function authHeaders(): Record<string, string> {
	return token ? { authorization: `Bearer ${token}` } : {};
}
