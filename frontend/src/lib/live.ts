// Live change notifications, so the same note open on two devices stays in
// step instead of colliding at save time.

import { apiBase, authHeaders, isDesktop } from './runtime';
import { readSseStream } from './sse';

export interface LiveNoteEvent {
	type: 'note' | 'note_removed';
	path: string;
	revision?: string;
	/** The editor that made the change; we ignore the echo of our own saves. */
	origin?: string | null;
}

/** Connection health, for the sync engine and the offline badge. `live` means
 * the server said `ready`; `offline` means we are between retries. */
export type LiveState = 'connecting' | 'live' | 'offline';

/** Identifies this tab for the life of the page. Sent with every save so the
 * server can tell us apart from the other device. */
export const CLIENT_ID: string =
	typeof crypto !== 'undefined' && 'randomUUID' in crypto
		? crypto.randomUUID()
		: `c${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;

const MAX_BACKOFF = 30_000;

export interface ConnectOptions {
	onState?: (state: LiveState) => void;
	/** Fired on a 401 — the stored token was revoked or expired. */
	onAuthExpired?: () => void;
}

/**
 * Subscribe to vault changes. Returns a function that closes the stream.
 *
 * The web uses EventSource; the desktop app must send a bearer header, which
 * EventSource cannot, so it reads the same stream over fetch. Reconnection is
 * handled here with backoff in both cases — EventSource retries a failed auth
 * just as eagerly as a dropped connection, so its native retry is unused.
 */
export function connectLive(
	onEvent: (event: LiveNoteEvent) => void,
	options: ConnectOptions = {}
): () => void {
	return isDesktop
		? connectViaFetch(onEvent, options)
		: connectViaEventSource(onEvent, options);
}

function connectViaEventSource(
	onEvent: (event: LiveNoteEvent) => void,
	{ onState, onAuthExpired }: ConnectOptions
): () => void {
	if (typeof EventSource === 'undefined') return () => {};
	let source: EventSource | null = null;
	let timer: ReturnType<typeof setTimeout> | null = null;
	let backoff = 1000;
	let closed = false;

	const listen = (target: EventSource, type: LiveNoteEvent['type']) => {
		target.addEventListener(type, (event) => {
			try {
				const data = JSON.parse((event as MessageEvent).data) as Omit<LiveNoteEvent, 'type'>;
				onEvent({ ...data, type });
			} catch {
				// A malformed frame is not worth tearing the connection down for.
			}
		});
	};

	const open = () => {
		if (closed) return;
		onState?.('connecting');
		source = new EventSource('/api/live');
		// Reaching "ready" is what proves the connection works — reset the
		// backoff there rather than on open, which fires before auth is checked.
		source.addEventListener('ready', () => {
			backoff = 1000;
			onState?.('live');
		});
		listen(source, 'note');
		listen(source, 'note_removed');
		source.onerror = () => {
			source?.close();
			source = null;
			if (closed) return;
			onState?.('offline');
			// EventSource hides status codes, so a revoked session looks like
			// any outage — and this used to hammer-retry it forever. Before
			// each retry, ask the (unauthenticated) status route; only a
			// server that answers "not authenticated" stops the loop.
			timer = setTimeout(async () => {
				if (closed) return;
				try {
					const status = await fetch('/api/auth/status').then((r) => r.json());
					if (status.auth_required && !status.authenticated) {
						onAuthExpired?.();
						return;
					}
				} catch {
					// Unreachable — a real outage, keep retrying.
				}
				open();
			}, backoff);
			backoff = Math.min(backoff * 2, MAX_BACKOFF);
		};
	};

	open();
	return () => {
		closed = true;
		if (timer) clearTimeout(timer);
		source?.close();
	};
}

function connectViaFetch(
	onEvent: (event: LiveNoteEvent) => void,
	{ onState, onAuthExpired }: ConnectOptions
): () => void {
	let controller: AbortController | null = null;
	let timer: ReturnType<typeof setTimeout> | null = null;
	let backoff = 1000;
	let closed = false;

	const retry = () => {
		if (closed) return;
		onState?.('offline');
		timer = setTimeout(open, backoff);
		backoff = Math.min(backoff * 2, MAX_BACKOFF);
	};

	const open = async () => {
		if (closed) return;
		onState?.('connecting');
		controller = new AbortController();
		try {
			const response = await fetch(`${apiBase()}/api/live`, {
				headers: { accept: 'text/event-stream', ...authHeaders() },
				signal: controller.signal
			});
			if (response.status === 401) {
				onState?.('offline');
				onAuthExpired?.();
				return; // Retrying with the same dead token is pointless.
			}
			if (!response.ok) throw new Error(`live ${response.status}`);
			await readSseStream(response, ({ event, data }) => {
				if (event === 'ready') {
					backoff = 1000;
					onState?.('live');
					return;
				}
				if (event !== 'note' && event !== 'note_removed') return;
				try {
					onEvent({ ...JSON.parse(data), type: event } as LiveNoteEvent);
				} catch {
					// A malformed frame is not worth tearing the connection down for.
				}
			});
			// Server closed the stream cleanly — reconnect.
			retry();
		} catch {
			if (!closed) retry();
		}
	};

	void open();
	return () => {
		closed = true;
		if (timer) clearTimeout(timer);
		controller?.abort();
	};
}
