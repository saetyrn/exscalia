// @vitest-environment jsdom
// Where focus goes when the OMNIA dropdown opens and closes. Recipes opens it
// with K, so it asks for the composer to take the keys typed next and for
// Escape to hand focus back; the media hub asks for neither and keeps its
// focus where it was.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./ChatPanel.svelte', () => import('./testing/ComposerStub.svelte'));
vi.mock('$lib/stores.svelte', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/stores.svelte')>();
	return {
		...actual,
		restoreNoteThread: vi.fn(async () => {}),
		restoreMediaHubThread: vi.fn(async () => {})
	};
});

const { default: MediaChatPopover } = await import('./MediaChatPopover.svelte');
const { overseerStatus } = await import('$lib/stores.svelte');

let target: HTMLElement;
let opener: HTMLButtonElement;
let instance: ReturnType<typeof mount> | undefined;
let props: { open: boolean; focusComposer: boolean };

function render(focusComposer: boolean, phone = false) {
	// Reduced motion: jsdom has no Web Animations to play the transitions.
	vi.stubGlobal('matchMedia', (query: string) => ({
		matches:
			query.includes('reduce') ||
			(phone ? query.includes('max-width') : query.includes('min-width')),
		addEventListener() {},
		removeEventListener() {}
	}));
	const live = $state({ open: false, focusComposer });
	props = live;
	instance = mount(MediaChatPopover, {
		target,
		props: {
			get open() {
				return props.open;
			},
			set open(value: boolean) {
				props.open = value;
			},
			get focusComposer() {
				return props.focusComposer;
			},
			trigger: false,
			hub: { key: 'recipes:hub', title: 'Recipes' },
			media: false
		}
	});
	flushSync();
}

function escape() {
	const e = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
	(document.activeElement ?? document.body).dispatchEvent(e);
	flushSync();
	return e;
}

const composer = () => target.querySelector<HTMLTextAreaElement>('.omnia-pop form textarea');

beforeEach(() => {
	overseerStatus.available = true;
	target = document.body.appendChild(document.createElement('div'));
	opener = document.body.appendChild(document.createElement('button'));
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = undefined;
	target.remove();
	opener.remove();
	vi.unstubAllGlobals();
});

describe('the OMNIA dropdown and focus', () => {
	it('puts the caret in the composer as it opens, from a key or a button', () => {
		render(true);
		props.open = true;
		flushSync();
		expect(composer()).not.toBeNull();
		expect(document.activeElement).toBe(composer());
	});

	it('closes on Escape and hands focus back to the button that opened it', () => {
		render(true);
		opener.focus();
		props.open = true;
		flushSync();
		expect(document.activeElement).toBe(composer());
		expect(escape().defaultPrevented).toBe(true);
		expect(props.open).toBe(false);
		expect(document.activeElement).toBe(opener);
	});

	it('leaves focus on the page when it was opened from the page', () => {
		render(true);
		props.open = true;
		flushSync();
		escape();
		expect(props.open).toBe(false);
		expect(document.activeElement).toBe(document.body);
	});

	it('leaves focus alone when asked to (the media hub)', () => {
		render(false);
		opener.focus();
		props.open = true;
		flushSync();
		expect(composer()).not.toBeNull();
		expect(document.activeElement).toBe(opener);
		escape();
		expect(document.activeElement).toBe(opener);
	});

	// A phone raises its keyboard for a focused field; the sheet keeps the
	// focus every sheet opens with, and gives it back as every sheet does.
	it('leaves the phone sheet its own focus', async () => {
		render(true, true);
		opener.focus();
		props.open = true;
		flushSync();
		await Promise.resolve();
		const sheet = document.querySelector('.sheet-panel');
		expect(document.querySelector('.omnia-pop')).toBeNull();
		expect(sheet?.contains(document.activeElement)).toBe(true);
		expect(document.activeElement?.tagName).not.toBe('TEXTAREA');
		props.open = false;
		flushSync();
		expect(document.activeElement).toBe(opener);
	});
});
