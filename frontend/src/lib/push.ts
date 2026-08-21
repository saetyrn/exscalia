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
	const sub =
		(await reg.pushManager.getSubscription()) ??
		(await reg.pushManager.subscribe({
			userVisibleOnly: true,
			applicationServerKey: urlBase64ToUint8Array(key)
		}));
	const json = sub.toJSON();
	if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
		throw new Error('The subscription was incomplete — try again.');
	}
	await api.subscribePush({
		endpoint: json.endpoint,
		keys: { p256dh: json.keys.p256dh, auth: json.keys.auth }
	});
}

export async function disablePush(): Promise<void> {
	if (!pushSupported()) return;
	const reg = await navigator.serviceWorker.ready.catch(() => null);
	const sub = reg ? await reg.pushManager.getSubscription() : null;
	if (!sub) return;
	const { endpoint } = sub;
	await sub.unsubscribe().catch(() => {});
	await api.unsubscribePush(endpoint).catch(() => {});
}
