// Live change notifications, so the same note open on two devices stays in
// step instead of colliding at save time.

import { apiBase, authHeaders, isDesktop } from './runtime';
import { readSseStream } from './sse';

export interface LiveNoteEvent {
	/** `paper` carries a handwriting page change: `path` is the vault-relative
	 * page directory and `revision` is the page's numeric revision.
	 * `note_moved` is a rename or move from `path` to `new_path`; the server
	 * still follows it with note_removed(path) + note(new_path) for older
	 * clients, so listeners that only know those two stay correct. */
	type: 'note' | 'note_removed' | 'note_moved' | 'paper';
	path: string;
	new_path?: string;
	revision?: string | number;
	/** The editor that made the change; we ignore the echo of our own saves. */
	origin?: string | null;
}

/** Both transports subscribe to exactly these; one list so they can't drift. */
const EVENT_TYPES: readonly LiveNoteEvent['type'][] = ['note', 'note_removed', 'note_moved', 'paper'];

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

/** The server sends a `ping` every 20 s while nothing else happens. Silence for
 * two of those plus slack means the socket is dead even though the browser has
 * not noticed — a laptop that slept, a NAT or radio that forgot the flow. */
export const WATCHDOG_MS = 45_000;

export interface ConnectOptions {
	onState?: (state: LiveState) => void;
	/** Fired on a 401 — the stored token was revoked or expired. */
	onAuthExpired?: () => void;
}

/** Closes the stream. The desktop connection also carries `kick`, which
 * drops the current attempt (or a pending retry) and reconnects at once with
 * the backoff reset: the sync engine calls it when the network comes back. */
export type LiveConnection = (() => void) & { kick?: () => void };

/**
 * Subscribe to vault changes. Returns a function that closes the stream.
 *
 * The web uses EventSource; the desktop app must send a bearer header, which
 * EventSource cannot, so it reads the same stream over fetch. Reconnection is
 * handled here with backoff in both cases — EventSource retries a failed auth
 * just as eagerly as a dropped connection, so its native retry is unused.
 * Both transports also reconnect when the stream goes silent past
 * WATCHDOG_MS, and skip the backoff when the network or the page comes back.
 */
export function connectLive(
	onEvent: (event: LiveNoteEvent) => void,
	options: ConnectOptions = {}
): LiveConnection {
	return isDesktop
		? connectViaFetch(onEvent, options)
		: connectViaEventSource(onEvent, options);
}

/** Run `wake` when the network returns or the page is shown again — the two
 * moments a stream waiting out its backoff should retry now. Returns the
 * remover. */
function onWake(wake: () => void): () => void {
	if (typeof window === 'undefined' || typeof document === 'undefined') return () => {};
	const onVisible = () => {
		if (document.visibilityState === 'visible') wake();
	};
	window.addEventListener('online', wake);
	document.addEventListener('visibilitychange', onVisible);
	return () => {
		window.removeEventListener('online', wake);
		document.removeEventListener('visibilitychange', onVisible);
	};
}

function connectViaEventSource(
	onEvent: (event: LiveNoteEvent) => void,
	{ onState, onAuthExpired }: ConnectOptions
): () => void {
	if (typeof EventSource === 'undefined') return () => {};
	let source: EventSource | null = null;
	let timer: ReturnType<typeof setTimeout> | null = null;
	let watchdog: ReturnType<typeof setTimeout> | null = null;
	let lastFrame = 0;
	let backoff = 1000;
	let closed = false;
	// Bumped by every open(), so an auth check still in flight when a wake has
	// already reconnected cannot open a second stream.
	let attempt = 0;

	// Every frame, heartbeats included, proves the socket still carries data.
	const bump = () => {
		lastFrame = Date.now();
		if (watchdog) clearTimeout(watchdog);
		watchdog = setTimeout(stalled, WATCHDOG_MS);
	};

	const drop = () => {
		if (watchdog) clearTimeout(watchdog);
		watchdog = null;
		source?.close();
		source = null;
	};

	// Nothing has arrived for too long: the connection is half-open and
	// EventSource would wait on it forever. Replace it now.
	const stalled = () => {
		drop();
		if (closed) return;
		onState?.('offline');
		backoff = 1000;
		open();
	};

	const listen = (target: EventSource, type: LiveNoteEvent['type']) => {
		target.addEventListener(type, (event) => {
			bump();
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
		if (timer) clearTimeout(timer);
		timer = null;
		const current = ++attempt;
		onState?.('connecting');
		const es = new EventSource('/api/live');
		source = es;
		bump();
		// Reaching "ready" is what proves the connection works — reset the
		// backoff there rather than on open, which fires before auth is checked.
		es.addEventListener('ready', () => {
			bump();
			backoff = 1000;
			onState?.('live');
		});
		es.addEventListener('ping', bump);
		for (const type of EVENT_TYPES) listen(es, type);
		es.onerror = () => {
			if (source !== es) return; // Already replaced.
			drop();
			if (closed) return;
			onState?.('offline');
			// EventSource hides status codes, so a revoked session looks like
			// any outage — and this used to hammer-retry it forever. Before
			// each retry, ask the (unauthenticated) status route; only a
			// server that answers "not authenticated" stops the loop.
			timer = setTimeout(async () => {
				timer = null;
				if (closed) return;
				let signedOut = false;
				try {
					const status = await fetch('/api/auth/status').then((r) => r.json());
					signedOut = status.auth_required && !status.authenticated;
				} catch {
					// Unreachable — a real outage, keep retrying.
				}
				// A wake may have reconnected while the check was out.
				if (closed || current !== attempt) return;
				if (signedOut) {
					// For good: the layout reconnects afresh after re-login.
					closed = true;
					onAuthExpired?.();
					return;
				}
				open();
			}, backoff);
			backoff = Math.min(backoff * 2, MAX_BACKOFF);
		};
	};

	const stopWake = onWake(() => {
		if (closed) return;
		if (!source) {
			// Between attempts (waiting out the backoff, or checking auth).
			backoff = 1000;
			open();
		} else if (Date.now() - lastFrame > WATCHDOG_MS) {
			// Timers stand still while a laptop sleeps, so the watchdog may
			// not have caught up with a socket that died meanwhile.
			stalled();
		}
	});

	open();
	return () => {
		closed = true;
		stopWake();
		if (timer) clearTimeout(timer);
		drop();
	};
}

function connectViaFetch(
	onEvent: (event: LiveNoteEvent) => void,
	{ onState, onAuthExpired }: ConnectOptions
): LiveConnection {
	let controller: AbortController | null = null;
	let timer: ReturnType<typeof setTimeout> | null = null;
	let watchdog: ReturnType<typeof setTimeout> | null = null;
	let lastFrame = 0;
	let backoff = 1000;
	let closed = false;

	// Same watchdog and wake as the EventSource path above. Any chunk counts,
	// keepalive comments included: a half-open socket after a sleep never
	// errors, it only goes quiet.
	const bump = () => {
		lastFrame = Date.now();
		if (watchdog) clearTimeout(watchdog);
		watchdog = setTimeout(stalled, WATCHDOG_MS);
	};

	// Abandon the current attempt; its pending read rejects, and the attempt
	// sees it is no longer current and stays quiet.
	const drop = () => {
		if (watchdog) clearTimeout(watchdog);
		watchdog = null;
		const dead = controller;
		controller = null;
		dead?.abort();
	};

	const stalled = () => {
		drop();
		if (closed) return;
		onState?.('offline');
		backoff = 1000;
		void open();
	};

	const retry = () => {
		drop();
		if (closed) return;
		onState?.('offline');
		timer = setTimeout(open, backoff);
		backoff = Math.min(backoff * 2, MAX_BACKOFF);
	};

	const open = async () => {
		if (closed) return;
		if (timer) clearTimeout(timer);
		timer = null;
		onState?.('connecting');
		const mine = new AbortController();
		controller = mine;
		// Armed before the request too: a fetch into a dead socket can hang
		// just as long as a stream that went silent.
		bump();
		try {
			const response = await fetch(`${apiBase()}/api/live`, {
				headers: { accept: 'text/event-stream', ...authHeaders() },
				signal: mine.signal
			});
			if (controller !== mine) return;
			if (response.status === 401) {
				drop();
				closed = true; // Retrying with the same dead token is pointless.
				onState?.('offline');
				onAuthExpired?.();
				return;
			}
			if (!response.ok) throw new Error(`live ${response.status}`);
			await readSseStream(
				response,
				({ event, data }) => {
					if (controller !== mine) return;
					bump();
					if (event === 'ready') {
						backoff = 1000;
						onState?.('live');
						return;
					}
					const type = EVENT_TYPES.find((known) => known === event);
					if (!type) return;
					try {
						onEvent({ ...JSON.parse(data), type } as LiveNoteEvent);
					} catch {
						// A malformed frame is not worth tearing the connection down for.
					}
				},
				() => {
					if (controller === mine) bump();
				}
			);
			// Server closed the stream cleanly — reconnect.
			if (controller === mine) retry();
		} catch {
			// A kick() or the watchdog already replaced this attempt; the new
			// one owns the state.
			if (controller === mine) retry();
		}
	};

	/** Reconnect now: the sync engine's answer to the network coming back,
	 * when the old socket is suspect and a backoff is only a delay. */
	const kick = () => {
		if (closed) return;
		if (timer) clearTimeout(timer);
		timer = null;
		backoff = 1000;
		drop();
		void open();
	};

	const stopWake = onWake(() => {
		if (closed) return;
		if (!controller) {
			backoff = 1000;
			void open();
		} else if (Date.now() - lastFrame > WATCHDOG_MS) {
			stalled();
		}
	});

	void open();
	const stop = () => {
		closed = true;
		stopWake();
		if (timer) clearTimeout(timer);
		drop();
	};
	return Object.assign(stop, { kick });
}
