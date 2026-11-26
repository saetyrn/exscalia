// The state machine that keeps server normalization out of a live IME
// composition. No DOM and no CodeMirror: the queue takes its "am I composing?"
// answer and its timers as callbacks, which is exactly what makes the
// interesting cases — a composition that never ends, a composition that ends
// straight into the next one — reachable from a test.
import { describe, expect, it, vi } from 'vitest';
import {
	CompositionQueue,
	COMPOSITION_HOLD_MS,
	reconcileHeld,
	settleHeld,
	type HeldReplacement
} from './composequeue';

/** A queue plus the knobs a test needs: a composing flag it can flip, the list
 *  of values that reached `apply`, and a hand-cranked timer. */
function harness(holdMs = COMPOSITION_HOLD_MS) {
	const applied: string[] = [];
	let composing = false;
	let scheduled: { fn: () => void; ms: number } | null = null;
	const clearTimer = vi.fn(() => {
		scheduled = null;
	});
	const queue = new CompositionQueue<string>({
		composing: () => composing,
		apply: (value) => applied.push(value),
		setTimer: (fn, ms) => {
			scheduled = { fn, ms };
			return 1 as unknown as ReturnType<typeof setTimeout>;
		},
		clearTimer,
		holdMs
	});
	return {
		queue,
		applied,
		clearTimer,
		compose(on: boolean) {
			composing = on;
		},
		get scheduled() {
			return scheduled;
		},
		/** Run whatever the hold cap scheduled, as if `holdMs` had elapsed. */
		fireCap() {
			const timer = scheduled;
			if (!timer) throw new Error('nothing scheduled');
			scheduled = null;
			timer.fn();
		}
	};
}

describe('CompositionQueue', () => {
	it('applies immediately and queues nothing when no composition is running', () => {
		const h = harness();
		expect(h.queue.request('one')).toBe(true);
		expect(h.applied).toEqual(['one']);
		expect(h.queue.pending).toBe(false);
		expect(h.scheduled).toBeNull();
	});

	it('holds a replacement requested while composing', () => {
		const h = harness();
		h.compose(true);
		expect(h.queue.request('server text')).toBe(false);
		expect(h.applied).toEqual([]);
		expect(h.queue.pending).toBe(true);
		expect(h.queue.pendingValue).toBe('server text');
	});

	it('applies the held value once the composition ends', () => {
		const h = harness();
		h.compose(true);
		h.queue.request('server text');
		h.compose(false);
		expect(h.queue.release()).toBe(true);
		expect(h.applied).toEqual(['server text']);
		expect(h.queue.pending).toBe(false);
	});

	it('applies only the newest of several values held during one composition', () => {
		const h = harness();
		h.compose(true);
		h.queue.request('first');
		h.queue.request('second');
		h.queue.request('third');
		h.compose(false);
		h.queue.release();
		expect(h.applied).toEqual(['third']);
	});

	it('starts the hold cap on the first held request and never extends it', () => {
		const h = harness(2000);
		h.compose(true);
		h.queue.request('first');
		const started = h.scheduled;
		expect(started?.ms).toBe(2000);
		h.queue.request('second');
		// Same scheduled callback: a stream of updates during a long composition
		// must not keep pushing the deadline out.
		expect(h.scheduled).toBe(started);
	});

	it('force-applies when the cap fires even though the composition is still live', () => {
		const h = harness();
		h.compose(true);
		h.queue.request('server text');
		h.fireCap();
		expect(h.applied).toEqual(['server text']);
		expect(h.queue.pending).toBe(false);
	});

	it('does nothing on release when the composition is still running', () => {
		const h = harness();
		h.compose(true);
		h.queue.request('server text');
		// Android fires compositionend per character, straight into the next
		// composition; the flag is the authority, not the event.
		expect(h.queue.release()).toBe(false);
		expect(h.applied).toEqual([]);
		expect(h.queue.pending).toBe(true);
	});

	it('is a no-op to release with nothing held', () => {
		const h = harness();
		expect(h.queue.release()).toBe(false);
		expect(h.applied).toEqual([]);
	});

	it('survives repeated releases without applying twice', () => {
		const h = harness();
		h.compose(true);
		h.queue.request('server text');
		h.compose(false);
		h.queue.release();
		h.queue.release();
		expect(h.applied).toEqual(['server text']);
	});

	it('cancels the cap when the held value is applied', () => {
		const h = harness();
		h.compose(true);
		h.queue.request('server text');
		h.compose(false);
		h.queue.release();
		expect(h.clearTimer).toHaveBeenCalled();
		expect(h.scheduled).toBeNull();
	});

	it('clears a hold when a later request arrives with nothing composing', () => {
		const h = harness();
		h.compose(true);
		h.queue.request('stale');
		h.compose(false);
		expect(h.queue.request('fresh')).toBe(true);
		expect(h.applied).toEqual(['fresh']);
		expect(h.queue.pending).toBe(false);
		expect(h.scheduled).toBeNull();
	});

	it('drops the held value on cancel — the document it described is gone', () => {
		const h = harness();
		h.compose(true);
		h.queue.request('other note');
		h.queue.cancel();
		h.compose(false);
		expect(h.queue.release()).toBe(false);
		expect(h.applied).toEqual([]);
		expect(h.scheduled).toBeNull();
	});

	it('holds again after a completed round trip', () => {
		const h = harness();
		h.compose(true);
		h.queue.request('one');
		h.compose(false);
		h.queue.release();
		h.compose(true);
		h.queue.request('two');
		expect(h.applied).toEqual(['one']);
		expect(h.scheduled).not.toBeNull();
		h.compose(false);
		h.queue.release();
		expect(h.applied).toEqual(['one', 'two']);
	});

	it('defaults the hold to two seconds', () => {
		expect(COMPOSITION_HOLD_MS).toBe(2000);
	});
});

// What a held replacement turns into once the IME lets go. The target was
// computed against `base`; the composition kept committing text meanwhile.
describe('reconcileHeld', () => {
	const BASE = ['---', 'modified: 1', '---', 'alpha line', 'beta line', ''].join('\n');

	it('applies the target as-is when nothing was composed during the hold', () => {
		const target = BASE.replace('modified: 1', 'modified: 2');
		expect(reconcileHeld(BASE, BASE, target)).toEqual({ text: target, ok: true });
	});

	it('keeps a word composed elsewhere alongside the replacement', () => {
		const current = BASE.replace('alpha line', 'alpha 日本語 line');
		const target = BASE.replace('beta line', 'beta line, from OMNIA').replace(
			'modified: 1',
			'modified: 2'
		);
		const { text: result, ok } = reconcileHeld(BASE, current, target);
		expect(ok).toBe(true);
		expect(result).toContain('alpha 日本語 line');
		expect(result).toContain('beta line, from OMNIA');
		expect(result).toContain('modified: 2');
		expect(result).toBe(
			['---', 'modified: 2', '---', 'alpha 日本語 line', 'beta line, from OMNIA', ''].join('\n')
		);
	});

	it('does not undo the typing when the held replacement changed nothing', () => {
		const current = BASE.replace('beta line', 'beta line 한국어');
		expect(reconcileHeld(BASE, current, BASE)).toEqual({ text: current, ok: true });
	});

	it('keeps what was composed when it overlaps the replacement, and says so', () => {
		const current = BASE.replace('beta line', 'beta 中文');
		const target = BASE.replace('beta line', 'beta row');
		expect(reconcileHeld(BASE, current, target)).toEqual({ text: current, ok: false });
	});
});

// What the page sees while a replacement waits. It has already taken the
// replacement's revision as its base, so it must see the replacement too, or
// the autosave of the composed text reverts it on the server.
describe('settleHeld', () => {
	const BASE = ['alpha line', 'beta line', ''].join('\n');
	const held = (doc: string, remote = true): HeldReplacement => ({
		base: BASE,
		doc,
		programmatic: true,
		remote
	});

	it('is the document itself with nothing held', () => {
		const onConflict = vi.fn();
		expect(settleHeld(undefined, BASE, onConflict)).toBe(BASE);
		expect(onConflict).not.toHaveBeenCalled();
	});

	it('includes the held replacement alongside what was composed', () => {
		const onConflict = vi.fn();
		const remote = BASE.replace('beta line', 'beta line, from the phone');
		const composed = BASE.replace('alpha line', 'alpha かな line');
		expect(settleHeld(held(remote), BASE, onConflict)).toBe(remote);
		expect(settleHeld(held(remote), composed, onConflict)).toBe(
			'alpha かな line\nbeta line, from the phone\n'
		);
		expect(onConflict).not.toHaveBeenCalled();
	});

	it('reports an overlap with a remote replacement once', () => {
		const onConflict = vi.fn();
		const value = held(BASE.replace('beta line', 'beta row'));
		const composed = BASE.replace('beta line', 'beta 中');
		expect(settleHeld(value, composed, onConflict)).toBe(composed);
		expect(settleHeld(value, composed.replace('中', '中文'), onConflict)).toBe(
			composed.replace('中', '中文')
		);
		expect(onConflict).toHaveBeenCalledOnce();
	});

	it('keeps the composition over an overlapping normalization without asking', () => {
		const onConflict = vi.fn();
		const composed = BASE.replace('beta line', 'beta 中');
		const value = held(BASE.replace('beta line', 'beta row'), false);
		expect(settleHeld(value, composed, onConflict)).toBe(composed);
		expect(onConflict).not.toHaveBeenCalled();
	});
});
