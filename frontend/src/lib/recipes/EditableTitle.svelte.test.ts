// @vitest-environment jsdom
// Rename in place: Enter or a blur saves, Escape cancels, and an empty or
// unchanged name saves nothing. A failed save keeps the field open with
// what was typed.
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import EditableTitle from './EditableTitle.svelte';

beforeAll(() => {
	// StretchedTitle measures itself; jsdom has no layout.
	vi.stubGlobal(
		'ResizeObserver',
		class {
			observe() {}
			disconnect() {}
		}
	);
});

let cleanup: (() => void) | undefined;
afterEach(() => {
	cleanup?.();
	cleanup = undefined;
});

function render(text: string, onrename: (title: string) => Promise<void>) {
	const target = document.body.appendChild(document.createElement('div'));
	const component = mount(EditableTitle, { target, props: { text, onrename } });
	flushSync();
	cleanup = () => {
		unmount(component);
		target.remove();
	};
	return target;
}

const box = (el: HTMLElement) => el.querySelector('.editable-title') as HTMLElement;
const field = (el: HTMLElement) => el.querySelector('input') as HTMLInputElement | null;

function open(el: HTMLElement): HTMLInputElement {
	box(el).dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
	flushSync();
	const input = field(el);
	if (!input) throw new Error('the field did not open');
	return input;
}

function type(input: HTMLInputElement, value: string) {
	input.value = value;
	input.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}

function key(input: HTMLInputElement, name: string): KeyboardEvent {
	const e = new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true });
	input.dispatchEvent(e);
	return e;
}

/** Let the save's promise and the component's updates land. */
async function settle() {
	await new Promise((r) => setTimeout(r, 0));
	await tick();
	flushSync();
}

describe('EditableTitle', () => {
	it('shows the title, and a double-click opens it with the name selected', () => {
		const el = render('Carbonara', vi.fn());
		expect(box(el).querySelector('h1')?.getAttribute('aria-label')).toBe('Carbonara');
		expect(field(el)).toBeNull();
		const input = open(el);
		expect(input.value).toBe('Carbonara');
		expect(document.activeElement).toBe(input);
		expect([input.selectionStart, input.selectionEnd]).toEqual([0, 'Carbonara'.length]);
	});

	it('saves the trimmed name on Enter', async () => {
		const onrename = vi.fn(async () => {});
		const el = render('Carbonara', onrename);
		const input = open(el);
		type(input, '  Spaghetti Carbonara  ');
		const e = key(input, 'Enter');
		expect(e.defaultPrevented).toBe(true);
		await settle();
		expect(onrename).toHaveBeenCalledExactlyOnceWith('Spaghetti Carbonara');
		expect(field(el)).toBeNull();
	});

	it('saves on blur, once', async () => {
		const onrename = vi.fn(async () => {});
		const el = render('Carbonara', onrename);
		const input = open(el);
		type(input, 'Cacio e pepe');
		input.dispatchEvent(new FocusEvent('blur'));
		input.dispatchEvent(new FocusEvent('blur'));
		await settle();
		expect(onrename).toHaveBeenCalledExactlyOnceWith('Cacio e pepe');
		expect(field(el)).toBeNull();
	});

	it('cancels on Escape, and keeps Escape from going back', async () => {
		const onrename = vi.fn(async () => {});
		const el = render('Carbonara', onrename);
		const outside = vi.fn();
		window.addEventListener('keydown', outside);
		const input = open(el);
		type(input, 'Something else');
		const e = key(input, 'Escape');
		// The blur that follows the field's removal decides nothing.
		input.dispatchEvent(new FocusEvent('blur'));
		await settle();
		window.removeEventListener('keydown', outside);
		expect(e.defaultPrevented).toBe(true);
		expect(outside).not.toHaveBeenCalled();
		expect(onrename).not.toHaveBeenCalled();
		expect(field(el)).toBeNull();
	});

	it('saves nothing for an empty or unchanged name', async () => {
		const onrename = vi.fn(async () => {});
		const el = render('Carbonara', onrename);
		let input = open(el);
		type(input, '   ');
		key(input, 'Enter');
		await settle();
		expect(field(el)).toBeNull();
		input = open(el);
		type(input, 'Carbonara ');
		key(input, 'Enter');
		await settle();
		expect(field(el)).toBeNull();
		expect(onrename).not.toHaveBeenCalled();
	});

	it('stays open with what was typed when the save fails, and can try again', async () => {
		const onrename = vi
			.fn<(title: string) => Promise<void>>()
			.mockRejectedValueOnce(new Error('409'))
			.mockResolvedValueOnce(undefined);
		const el = render('Carbonara', onrename);
		const input = open(el);
		type(input, 'Carbonara for four');
		key(input, 'Enter');
		await settle();
		expect(onrename).toHaveBeenCalledTimes(1);
		expect(field(el)).toBe(input);
		expect(input.value).toBe('Carbonara for four');
		expect(input.disabled).toBe(false);
		key(input, 'Enter');
		await settle();
		expect(onrename).toHaveBeenCalledTimes(2);
		expect(onrename).toHaveBeenLastCalledWith('Carbonara for four');
		expect(field(el)).toBeNull();
	});
});
