// The pure half of use:modal: which element Tab lands on, which layer owns
// Escape, who holds the shell inert, and where focus goes back to on close.
import { describe, expect, it } from 'vitest';
import {
	FOCUSABLE,
	holdInert,
	LayerStack,
	nextFocusTarget,
	pickReturnTarget,
	releaseInert,
	type ReturnChain
} from './modal';

describe('FOCUSABLE', () => {
	const parts = FOCUSABLE.split(', ');

	it('covers every control a dialog, sheet or the drawer holds', () => {
		for (const base of ['a[href]', 'button:not([disabled])', 'select:not([disabled])', 'textarea:not([disabled])', '[tabindex]']) {
			expect(parts.some((part) => part.startsWith(base))).toBe(true);
		}
		expect(parts.some((part) => part.startsWith('input:not([disabled]):not([type="hidden"])'))).toBe(true);
	});

	it('skips tabindex="-1" everywhere, so a scrim or panel is never a Tab stop', () => {
		for (const part of parts) expect(part.endsWith(':not([tabindex="-1"])')).toBe(true);
	});
});

describe('nextFocusTarget', () => {
	const list = ['first', 'middle', 'last'];

	it('wraps Tab from the last stop to the first', () => {
		expect(nextFocusTarget(list, 'last', false)).toBe('first');
	});

	it('wraps Shift+Tab from the first stop to the last', () => {
		expect(nextFocusTarget(list, 'first', true)).toBe('last');
	});

	it('leaves steps between stops to the browser', () => {
		expect(nextFocusTarget(list, 'first', false)).toBeNull();
		expect(nextFocusTarget(list, 'middle', false)).toBeNull();
		expect(nextFocusTarget(list, 'middle', true)).toBeNull();
		expect(nextFocusTarget(list, 'last', true)).toBeNull();
	});

	it('pulls focus in from the panel itself in both directions', () => {
		// The leak: a confirm focuses its tabindex=-1 panel, and Shift+Tab from
		// there reached the scrim button behind it.
		expect(nextFocusTarget(list, 'panel', true)).toBe('last');
		expect(nextFocusTarget(list, 'panel', false)).toBe('first');
		expect(nextFocusTarget(list, null, false)).toBe('first');
	});

	it('keeps a single stop focused', () => {
		expect(nextFocusTarget(['only'], 'only', false)).toBe('only');
		expect(nextFocusTarget(['only'], 'only', true)).toBe('only');
	});

	it('has nothing to offer from an empty list', () => {
		expect(nextFocusTarget([], 'panel', false)).toBeNull();
	});
});

describe('LayerStack', () => {
	it('gives the most recent layer the top', () => {
		const stack = new LayerStack<string>();
		stack.push('A');
		stack.push('B');
		expect(stack.top()).toBe('B');
		stack.pop('B');
		expect(stack.top()).toBe('A');
		stack.pop('A');
		expect(stack.top()).toBeUndefined();
		expect(stack.size).toBe(0);
	});

	it('an empty stack has no top', () => {
		expect(new LayerStack<string>().top()).toBeUndefined();
	});

	it('keeps the top when a lower layer finishes closing late', () => {
		// The palette's outro outlives the prompt it opened.
		const stack = new LayerStack<string>();
		stack.push('palette');
		stack.push('prompt');
		stack.pop('palette');
		expect(stack.top()).toBe('prompt');
		expect(stack.has('palette')).toBe(false);
		expect(stack.size).toBe(1);
	});

	it('ignores a layer it does not hold', () => {
		const stack = new LayerStack<string>();
		stack.push('A');
		stack.pop('B');
		expect(stack.top()).toBe('A');
	});
});

describe('holdInert / releaseInert', () => {
	it('stays inert until the last holder lets go, in any order', () => {
		const shell = { inert: false };
		holdInert(shell);
		holdInert(shell);
		expect(shell.inert).toBe(true);
		releaseInert(shell);
		expect(shell.inert).toBe(true);
		releaseInert(shell);
		expect(shell.inert).toBe(false);
	});

	it('restores what was there before the first hold', () => {
		const already = { inert: true };
		holdInert(already);
		releaseInert(already);
		expect(already.inert).toBe(true);
	});

	it('ignores a release with no hold', () => {
		const shell = { inert: false };
		releaseInert(shell);
		expect(shell.inert).toBe(false);
	});
});

describe('pickReturnTarget', () => {
	const el = (isConnected = true) => ({ isConnected });
	const never = () => false;

	it('returns focus to where it came from', () => {
		const editor = el();
		expect(pickReturnTarget({ node: el(false), returnTo: editor }, never)).toBe(editor);
	});

	it('falls back along the chain when the opener is gone', () => {
		// Cmd-K from the editor, then "New folder": the prompt opened from the
		// palette input, which is detached by the time the prompt closes.
		const editor = el();
		const palette: ReturnChain<{ isConnected: boolean }> = { node: el(false), returnTo: editor };
		const prompt = { node: el(false), returnTo: el(false), via: palette };
		expect(pickReturnTarget(prompt, never)).toBe(editor);
	});

	it('stays inside a layer that is still open', () => {
		// A confirm opened from a chat card inside a sheet, and the card
		// re-rendered meanwhile: focus goes to the sheet, not behind it.
		const sheet: ReturnChain<{ isConnected: boolean }> = { node: el(), returnTo: el() };
		const confirm = { node: el(false), returnTo: el(false), via: sheet };
		expect(pickReturnTarget(confirm, (layer) => layer === sheet)).toBe(sheet.node);
	});

	it('gives up when nothing is left to return to', () => {
		expect(pickReturnTarget({ node: el(false), returnTo: null }, never)).toBeNull();
	});
});
