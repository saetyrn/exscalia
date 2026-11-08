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
	claimFirstOpen,
	confirmObservation,
	dismissObservation,
	localToday,
	refreshMorning
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

		await confirmObservation(OBS.id);
		expect(morning.message).toBe('Morning.');
	});
});

describe('the first open of the day', () => {
	const SEEN = 'exscalia.morning.seen';

	beforeEach(() => storage.clear());

	it('is claimed once per local date', () => {
		expect(claimFirstOpen()).toBe(true);
		expect(storage.get(SEEN)).toBe(localToday());
		expect(claimFirstOpen()).toBe(false);
	});

	it('comes round again on a new date', () => {
		storage.set(SEEN, '2000-01-01');
		expect(claimFirstOpen()).toBe(true);
	});
});

describe('fetching the briefing', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		morning.message = null;
		morning.briefing = null;
	});

	it('shares one request between overlapping calls', async () => {
		const pending = deferred<{ message: string }>();
		stub.morningCheck.mockReturnValue(pending.promise);
		const first = refreshMorning();
		const second = refreshMorning();
		pending.resolve({ message: 'Morning.' });
		await Promise.all([first, second]);
		expect(stub.morningCheck).toHaveBeenCalledTimes(1);
		expect(morning.message).toBe('Morning.');

		stub.morningCheck.mockResolvedValue({ message: 'Again.' });
		await refreshMorning();
		expect(stub.morningCheck).toHaveBeenCalledTimes(2);
	});
});
