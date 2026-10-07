// The morning briefing: OMNIA's greeting plus an at-a-glance day/week view.
//
// The greeting itself is still generated server-side at most once per calendar
// day. What changed here is the *showing*: the briefing re-appears on every
// open until the user dismisses it for the day, then stays hidden until the
// date rolls over. That gate lives entirely on the client — a per-day "seen"
// stamp in localStorage compared against the current local date, so a new day
// (or a session left open past midnight) shows it again with no server state.

import { api, ApiError, type DailyBriefing, type Observation } from '$lib/api';
import { localToday } from '$lib/dates';
import { activateOmniaThread } from '$lib/stores.svelte';

// Re-exported: the layout and the briefing read the greeting's day from here.
export { localToday };

const SEEN_KEY = 'exscalia.morning.seen';

function seenToday(): boolean {
	try {
		return localStorage.getItem(SEEN_KEY) === localToday();
	} catch {
		return false;
	}
}

export const morning = $state<{
	message: string | null;
	threadId: string | null;
	briefing: DailyBriefing | null;
	observation: Observation | null;
	visible: boolean;
}>({ message: null, threadId: null, briefing: null, observation: null, visible: false });

/** Fetch the briefing and show it unless it was already dismissed today.
 *  Safe to call on every app open and on a midnight rollover. `force` shows
 *  it even so — the user asked for it back. */
export async function refreshMorning({ force = false }: { force?: boolean } = {}): Promise<void> {
	try {
		const r = await api.morningCheck();
		if (r.thread_id) morning.threadId = r.thread_id;
		if (r.message) morning.message = r.message;
		if (r.briefing) morning.briefing = r.briefing;
		// Assigned unconditionally, unlike the fields above: the observation is
		// sent on every open, so an absent one is an answer (nothing staged) and
		// has to clear whatever was carried over.
		morning.observation = r.observation ?? null;
		morning.visible = (force || !seenToday()) && (!!morning.briefing || !!morning.message);
	} catch {
		// Offline or not signed in yet; leave the briefing hidden.
	}
}

/** Reopen today's briefing on request (the palette's "Today's briefing").
 *  The day's dismissal stamp stays: asking for it once must not re-arm the
 *  automatic showing on every open. */
export async function showMorning(): Promise<void> {
	if (morning.briefing || morning.message) {
		morning.visible = true;
		return;
	}
	await refreshMorning({ force: true });
}

/** Point the OMNIA chat at today's thread so a reply lands in the greeting's
 *  own conversation. Lazy — done only when the user opens the reply box, so a
 *  plain reload never steals whichever thread they had selected. */
export function activateMorningThread(): void {
	if (morning.threadId) activateOmniaThread(morning.threadId);
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

/** Dismiss for the rest of the local day. */
export function dismissMorning(): void {
	try {
		localStorage.setItem(SEEN_KEY, localToday());
	} catch {
		// Session-only dismissal when storage is unavailable.
	}
	morning.visible = false;
}
