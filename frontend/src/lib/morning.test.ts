// Answering the staged observation. The interesting part is not the request —
// it is what the row does while the request is in flight, and what it does when
// the request comes back wrong.
import { beforeEach, describe, expect, it, vi } from 'vitest';

const stub = vi.hoisted(() => ({
	confirmObservation: vi.fn(),
	dismissObservation: vi.fn(),
	morningCheck: vi.fn()
}));

vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, ...stub } };
});

// The per-day dismissal lives in localStorage, which node does not have.
const storage = new Map<string, string>();
vi.stubGlobal('localStorage', {
	getItem: (key: string) => storage.get(key) ?? null,
	setItem: (key: string, value: string) => void storage.set(key, value),
	removeItem: (key: string) => void storage.delete(key)
});

const { ApiError } = await import('$lib/api');
const {
	morning,
	confirmObservation,
	dismissObservation,
	dismissMorning,
	localToday,
	refreshMorning,
	showMorning
} = await import('./morning.svelte');

const OBS = {
	id: 'obs-1',
	text: 'You open the daily note before anything else.',
	kind: 'habit' as const,
	habit_key: 'daily_first',
	habit_value: 'yes',
	created_at: '2026-08-27T06:00:00Z'
};

/** A promise the test decides the fate of, so the row can be inspected mid-flight. */
function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

describe('answering an observation', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		morning.observation = { ...OBS };
	});

	it('clears the row before the request settles', async () => {
		const gate = deferred<{ ok: boolean }>();
		stub.confirmObservation.mockReturnValue(gate.promise);

		const done = confirmObservation(OBS.id);
		expect(morning.observation).toBe(null);
		expect(stub.confirmObservation).toHaveBeenCalledWith(OBS.id);

		gate.resolve({ ok: true });
		await done;
		expect(morning.observation).toBe(null);
	});

	it('dismisses through the dismiss endpoint', async () => {
		stub.dismissObservation.mockResolvedValue({ ok: true });

		await dismissObservation(OBS.id);
		expect(stub.dismissObservation).toHaveBeenCalledWith(OBS.id);
		expect(stub.confirmObservation).not.toHaveBeenCalled();
		expect(morning.observation).toBe(null);
	});

	it('treats a 404 as an answer — the observation merely expired first', async () => {
		stub.confirmObservation.mockRejectedValue(new ApiError(404, 'not found'));

		await confirmObservation(OBS.id);
		expect(morning.observation).toBe(null);
	});

	it('puts the row back when the request genuinely fails', async () => {
		stub.dismissObservation.mockRejectedValue(new ApiError(500, 'boom'));

		await dismissObservation(OBS.id);
		expect(morning.observation?.id).toBe(OBS.id);
	});

	it('ignores an id that is not the staged one', async () => {
		await confirmObservation('obs-stale');
		expect(stub.confirmObservation).not.toHaveBeenCalled();
		expect(morning.observation?.id).toBe(OBS.id);
	});

	it('leaves the rest of the briefing alone', async () => {
		stub.confirmObservation.mockResolvedValue({ ok: true });
		morning.message = 'Morning.';
		morning.visible = true;

		await confirmObservation(OBS.id);
		expect(morning.message).toBe('Morning.');
		expect(morning.visible).toBe(true);
	});
});

describe('reopening the briefing', () => {
	const SEEN = 'exscalia.morning.seen';
	const BRIEFING = {
		today_events: [],
		in_progress: [],
		week: [],
		stats: {}
	} as unknown as NonNullable<typeof morning.briefing>;

	beforeEach(() => {
		vi.clearAllMocks();
		storage.clear();
		morning.message = null;
		morning.briefing = null;
		morning.visible = false;
		stub.morningCheck.mockResolvedValue({ message: 'Morning.', briefing: BRIEFING });
	});

	it('shows it again after a dismissal without re-arming the daily showing', async () => {
		await refreshMorning();
		expect(morning.visible).toBe(true);
		dismissMorning();
		expect(morning.visible).toBe(false);

		await showMorning();
		expect(morning.visible).toBe(true);
		expect(storage.get(SEEN)).toBe(localToday());
		// Already in hand — no second round trip.
		expect(stub.morningCheck).toHaveBeenCalledTimes(1);

		// The next open still respects today's dismissal.
		morning.visible = false;
		await refreshMorning();
		expect(morning.visible).toBe(false);
	});

	it('fetches it first when nothing is loaded yet', async () => {
		storage.set(SEEN, localToday());

		await showMorning();
		expect(stub.morningCheck).toHaveBeenCalledTimes(1);
		expect(morning.message).toBe('Morning.');
		expect(morning.visible).toBe(true);
		expect(storage.get(SEEN)).toBe(localToday());
	});

	it('stays hidden when there is nothing to show', async () => {
		stub.morningCheck.mockResolvedValue({});

		await showMorning();
		expect(morning.visible).toBe(false);
	});
});
