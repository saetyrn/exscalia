// The morning check: OMNIA's greeting, which opens the day's conversation, and
// the observation it may have staged (shown above the OMNIA chat).
//
// The greeting itself is generated server-side at most once per calendar day.
// The first open on a new local date lands on today's daily note. That gate
// lives entirely on the client — a per-day stamp in localStorage compared
// against the current local date.

import { api, ApiError, type DailyBriefing, type Observation } from '$lib/api';
import { localToday } from '$lib/dates';

// Re-exported: the layout reads the greeting's day from here.
export { localToday };

const SEEN_KEY = 'exscalia.morning.seen';

/** True the first time it is asked on each local date, and stamps the date so
 *  every later ask that day is false. Without storage every open counts. */
export function claimFirstOpen(): boolean {
	try {
		if (localStorage.getItem(SEEN_KEY) === localToday()) return false;
		localStorage.setItem(SEEN_KEY, localToday());
	} catch {
		// Storage unavailable: nothing to remember the day by.
	}
	return true;
}

export const morning = $state<{
	message: string | null;
	threadId: string | null;
	briefing: DailyBriefing | null;
	observation: Observation | null;
}>({ message: null, threadId: null, briefing: null, observation: null });

let inflight: Promise<void> | null = null;

/** Fetch the briefing. Safe to call on every app open and on a midnight
 *  rollover: calls that overlap share one request. */
export function refreshMorning(): Promise<void> {
	inflight ??= load().finally(() => (inflight = null));
	return inflight;
}

async function load(): Promise<void> {
	try {
		const r = await api.morningCheck();
		if (r.thread_id) morning.threadId = r.thread_id;
		if (r.message) morning.message = r.message;
		if (r.briefing) morning.briefing = r.briefing;
		// Assigned unconditionally, unlike the fields above: the observation is
		// sent on every open, so an absent one is an answer (nothing staged) and
		// has to clear whatever was carried over.
		morning.observation = r.observation ?? null;
	} catch {
		// Offline or not signed in yet; keep what is already here.
	}
}

/** Answer the staged observation. The row goes the instant they click — the
 *  decision is the user's, and the round trip only records it — then the usual
 *  optimistic contract applies: a failure puts the row back to retry. A 404 is
 *  not a failure, it just means the observation expired before they answered,
 *  which lands in the same place as an answer. The rest of the briefing is
 *  untouched either way. */
async function answerObservation(
	id: string,
	act: (id: string) => Promise<{ ok: boolean }>
): Promise<void> {
	const staged = morning.observation;
	if (staged?.id !== id) return;
	morning.observation = null;
	try {
		await act(id);
	} catch (err) {
		if (err instanceof ApiError && err.status === 404) return;
		morning.observation = staged;
	}
}

/** Sign off on the observation — the server applies the learning. */
export function confirmObservation(id: string): Promise<void> {
	return answerObservation(id, api.confirmObservation);
}

/** Wave the observation away; nothing is learned. */
export function dismissObservation(id: string): Promise<void> {
	return answerObservation(id, api.dismissObservation);
}
