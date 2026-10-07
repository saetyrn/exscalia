// Web Push client: permission + subscription, registered with the server so
// OMNIA can reach this device when the app is closed. The service worker owns
// the receive/show side (src/service-worker.ts).
import { api } from './api';

/** VAPID keys are base64url; PushManager wants the raw bytes. */
function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
	const padding = '='.repeat((4 - (base64.length % 4)) % 4);
	const normalized = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/');
	const raw = atob(normalized);
	// Back it with a concrete ArrayBuffer so it satisfies BufferSource (a plain
	// `new Uint8Array(n)` widens to ArrayBufferLike under recent TS libs).
	const out = new Uint8Array(new ArrayBuffer(raw.length));
	for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
	return out;
}

export function pushSupported(): boolean {
	return (
		typeof window !== 'undefined' &&
		'Notification' in window &&
		'serviceWorker' in navigator &&
		'PushManager' in window
	);
}

/** iOS only allows Web Push from an installed/standalone PWA. */
export function isStandalone(): boolean {
	if (typeof window === 'undefined') return false;
	return (
		window.matchMedia?.('(display-mode: standalone)').matches === true ||
		(navigator as unknown as { standalone?: boolean }).standalone === true
	);
}

export type PushSnapshot = {
	supported: boolean;
	standalone: boolean;
	permission: NotificationPermission | 'unsupported';
	subscribed: boolean;
};

export async function pushState(): Promise<PushSnapshot> {
	if (!pushSupported()) {
		return {
			supported: false,
			standalone: isStandalone(),
			permission: 'unsupported',
			subscribed: false
		};
	}
	const reg = await navigator.serviceWorker.ready.catch(() => null);
	const sub = reg ? await reg.pushManager.getSubscription() : null;
	return {
		supported: true,
		standalone: isStandalone(),
		permission: Notification.permission,
		subscribed: !!sub
	};
}

/** This device opted in to push. Distinguishes "the browser lost the
 * subscription" (re-subscribe silently — permission is already granted) from
 * "the user turned it off" (leave it off). */
const PUSH_ON_KEY = 'exscalia.push.on';

function rememberPushOn(on: boolean): void {
	try {
		if (on) localStorage.setItem(PUSH_ON_KEY, '1');
		else localStorage.removeItem(PUSH_ON_KEY);
	} catch {
		// Storage unavailable: sync falls back to healing an existing sub only.
	}
}

function pushIntentOn(): boolean {
	try {
		return localStorage.getItem(PUSH_ON_KEY) === '1';
	} catch {
		return false;
	}
}

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
	return a.length === b.length && a.every((v, i) => v === b[i]);
}

/** A subscription for the server's current VAPID key. One made under another
 * key can never be delivered to again (the push service checks every send
 * against the key it was created with), so it is replaced instead of being
 * re-registered forever after an accidental server key change. */
async function boundSubscription(
	reg: ServiceWorkerRegistration,
	existing: PushSubscription | null,
	key: string
): Promise<PushSubscription> {
	const serverKey = urlBase64ToUint8Array(key);
	if (existing) {
		const bound = existing.options?.applicationServerKey;
		if (!bound || sameBytes(new Uint8Array(bound), serverKey)) return existing;
		const { endpoint } = existing;
		await existing.unsubscribe().catch(() => {});
		await api.unsubscribePush(endpoint).catch(() => {});
	}
	return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: serverKey });
}

async function registerWithServer(sub: PushSubscription): Promise<void> {
	const json = sub.toJSON();
	if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
		throw new Error('The subscription was incomplete — try again.');
	}
	await api.subscribePush({
		endpoint: json.endpoint,
		keys: { p256dh: json.keys.p256dh, auth: json.keys.auth }
	});
}

/** Request permission (MUST be called from a user gesture), subscribe, and
 * register the subscription with the server. Throws a human-readable message on
 * failure so the Settings UI can surface it. */
export async function enablePush(): Promise<void> {
	if (!pushSupported()) throw new Error('This browser does not support notifications.');
	const permission = await Notification.requestPermission();
	if (permission !== 'granted') {
		throw new Error('Notification permission was denied — enable it in your browser settings.');
	}
	const { key } = await api.getPushKey();
	if (!key) throw new Error('The server has no push key configured.');
	const reg = await navigator.serviceWorker.ready;
	const sub = await boundSubscription(reg, await reg.pushManager.getSubscription(), key);
	await registerWithServer(sub);
	rememberPushOn(true);
}

export async function disablePush(): Promise<void> {
	rememberPushOn(false);
	if (!pushSupported()) return;
	const reg = await navigator.serviceWorker.ready.catch(() => null);
	const sub = reg ? await reg.pushManager.getSubscription() : null;
	if (!sub) return;
	const { endpoint } = sub;
	await sub.unsubscribe().catch(() => {});
	await api.unsubscribePush(endpoint).catch(() => {});
}

/** Re-register this device's subscription on app open. The server prunes a
 * subscription the moment its endpoint dies (404/410 — endpoint rotation, a
 * PWA reinstall), and nothing ever told it about the device again: push
 * silently stayed dead while Settings still showed it enabled. The register
 * call is an idempotent upsert keyed by endpoint, so healing is a key check
 * and one small POST per boot. Quietly does nothing without permission or
 * opt-in. */
export async function syncPush(): Promise<void> {
	if (!pushSupported() || Notification.permission !== 'granted') return;
	try {
		const reg = await navigator.serviceWorker.ready;
		let sub = await reg.pushManager.getSubscription();
		// Permission is granted but the subscription is gone. Only rebuild it
		// when this device chose push — a granted permission alone may predate
		// the user turning notifications off.
		if (!sub && !pushIntentOn()) return;
		const { key } = await api.getPushKey();
		// No key means push is off server-side (an unreadable key file); leave
		// the device's subscription as it is until the key comes back.
		if (key) sub = await boundSubscription(reg, sub, key);
		if (!sub) return;
		await registerWithServer(sub);
		rememberPushOn(true);
	} catch {
		// Never let sync noise break app startup; the next open retries.
	}
}
