// Toast lifetimes. A toast with an action or an error lingers, a sticky one
// waits for the user, and hover/focus holds the clock rather than resetting it.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { dismissToast, notify, pauseToast, resumeToast, ui } from './ui.svelte';

// The store schedules through window.setTimeout; under node the global is it.
vi.stubGlobal('window', globalThis);

function latest() {
	return ui.toasts[ui.toasts.length - 1];
}

function present(id: number) {
	return ui.toasts.some((toast) => toast.id === id);
}

describe('toast lifetime', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		ui.toasts.splice(0);
	});

	afterEach(() => {
		vi.clearAllTimers();
		vi.useRealTimers();
	});

	it('dismisses a plain toast after the short default', () => {
		notify('Saved');
		const { id } = latest();
		vi.advanceTimersByTime(4100);
		expect(present(id)).toBe(true);
		vi.advanceTimersByTime(200);
		expect(present(id)).toBe(false);
	});

	it('keeps a toast with an action for twelve seconds', () => {
		notify('Standup in 5 min', { actionLabel: 'Calendar', action: () => {} });
		const { id } = latest();
		vi.advanceTimersByTime(5000);
		expect(present(id)).toBe(true);
		vi.advanceTimersByTime(6900);
		expect(present(id)).toBe(true);
		vi.advanceTimersByTime(100);
		expect(present(id)).toBe(false);
	});

	it('keeps an error toast for twelve seconds', () => {
		notify('Save failed', { tone: 'error' });
		const { id } = latest();
		vi.advanceTimersByTime(11_900);
		expect(present(id)).toBe(true);
		vi.advanceTimersByTime(100);
		expect(present(id)).toBe(false);
	});

	it('honours an explicit timeout over the defaults', () => {
		notify('Kept both', { actionLabel: 'Open', action: () => {}, timeout: 9000 });
		const { id } = latest();
		vi.advanceTimersByTime(8900);
		expect(present(id)).toBe(true);
		vi.advanceTimersByTime(100);
		expect(present(id)).toBe(false);
	});

	it('holds while paused and resumes with the time that was left', () => {
		notify('Update 1.2.3 is available.', { actionLabel: 'Install', action: () => {} });
		const { id } = latest();
		vi.advanceTimersByTime(3000);
		pauseToast(id);
		vi.advanceTimersByTime(30_000);
		expect(present(id)).toBe(true);

		resumeToast(id);
		vi.advanceTimersByTime(8900);
		expect(present(id)).toBe(true);
		vi.advanceTimersByTime(100);
		expect(present(id)).toBe(false);
	});

	it('gives a resumed toast at least a moment before it goes', () => {
		notify('Saved');
		const { id } = latest();
		vi.advanceTimersByTime(4100);
		pauseToast(id);
		resumeToast(id);
		vi.advanceTimersByTime(1400);
		expect(present(id)).toBe(true);
		vi.advanceTimersByTime(100);
		expect(present(id)).toBe(false);
	});

	it('treats a repeated pause or an unpaused resume as a no-op', () => {
		notify('Saved');
		const { id } = latest();
		vi.advanceTimersByTime(1000);
		resumeToast(id);
		pauseToast(id);
		vi.advanceTimersByTime(1000);
		pauseToast(id);
		resumeToast(id);
		vi.advanceTimersByTime(3100);
		expect(present(id)).toBe(true);
		vi.advanceTimersByTime(100);
		expect(present(id)).toBe(false);
	});

	it('never dismisses a sticky toast on its own', () => {
		notify('Update 1.2.3 is available.', { actionLabel: 'Install', action: () => {}, timeout: 0 });
		const { id } = latest();
		expect(vi.getTimerCount()).toBe(0);
		vi.advanceTimersByTime(60 * 60 * 1000);
		pauseToast(id);
		resumeToast(id);
		vi.advanceTimersByTime(60 * 60 * 1000);
		expect(present(id)).toBe(true);

		dismissToast(id);
		expect(present(id)).toBe(false);
	});

	it('cancels the pending timer when a toast is dismissed by hand', () => {
		notify('Saved');
		const { id } = latest();
		expect(vi.getTimerCount()).toBe(1);
		dismissToast(id);
		expect(vi.getTimerCount()).toBe(0);
	});
});
