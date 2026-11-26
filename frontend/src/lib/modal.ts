// One focus/Escape primitive for every dialog, sheet and drawer.
//
// Each overlay used to carry its own slice of this: two Tab traps that let
// Shift+Tab out of a focused panel, four dialogs with no trap or initial focus,
// window Escape handlers that all fired at once, and nothing anywhere that put
// focus back — so every palette or dialog round-trip left the caret on <body>
// (the editor's blur guard cannot help: the palette input is a real element).
// `use:modal` does it once:
//
// - records the focused element on mount and hands focus back on destroy, so
//   Cmd-K → Esc returns the caret to the editor;
// - focuses `initialFocus()`, else the first tabbable, else the node itself;
// - traps Tab in both directions, including from the focused node itself;
// - joins one layer stack: a single window listener gives Escape and Tab to
//   the topmost modal only, so closing a dialog over a sheet leaves the sheet;
// - optionally makes `inert` the shell behind a layout-level dialog.
//
// The pure parts (selector, focus order, the stack, inert counts, the return
// chain) touch no window/document at import, so they unit-test in node.
import type { ActionReturn } from 'svelte/action';

const SKIP = ':not([tabindex="-1"])';

/** Everything Tab can land on. A `tabindex="-1"` element is focusable but not
 * tabbable, so it can never be the first/last stop the trap wraps between. */
export const FOCUSABLE = [
	'a[href]',
	'button:not([disabled])',
	'input:not([disabled]):not([type="hidden"])',
	'select:not([disabled])',
	'textarea:not([disabled])',
	'[tabindex]'
]
	.map((selector) => selector + SKIP)
	.join(', ');

/** Where Tab should move focus inside a modal, or null to let the browser take
 * the step itself. `active` outside the list — the panel, <body>, anything
 * behind — wraps to the first stop (last for Shift+Tab); that case is the leak
 * the old traps had, which only ever matched `first`/`last`. */
export function nextFocusTarget<T>(list: readonly T[], active: unknown, shiftKey: boolean): T | null {
	if (list.length === 0) return null;
	const first = list[0];
	const last = list[list.length - 1];
	const index = list.indexOf(active as T);
	if (index === -1) return shiftKey ? last : first;
	if (shiftKey && index === 0) return last;
	if (!shiftKey && index === list.length - 1) return first;
	return null;
}

/** Open modals, oldest first. Removal is by identity rather than a plain pop:
 * teardown is not always last-in-first-out — an outro keeps a closing layer
 * mounted for a beat after the next one has opened. */
export class LayerStack<T> {
	private items: T[] = [];

	get size(): number {
		return this.items.length;
	}

	push(item: T): void {
		this.items.push(item);
	}

	pop(item: T): void {
		const index = this.items.lastIndexOf(item);
		if (index >= 0) this.items.splice(index, 1);
	}

	top(): T | undefined {
		return this.items[this.items.length - 1];
	}

	has(item: T): boolean {
		return this.items.includes(item);
	}

	find(match: (item: T) => boolean): T | undefined {
		return this.items.find(match);
	}
}

type Inertable = { inert: boolean };
const inertHolds = new WeakMap<Inertable, { count: number; was: boolean }>();

/** Make `target` inert until every holder has released it. Counted, not
 * saved-and-restored per holder: two dialogs closing out of order would
 * otherwise leave the shell stuck inert (or un-inert one still open). */
export function holdInert(target: Inertable): void {
	const hold = inertHolds.get(target);
	if (hold) {
		hold.count++;
		return;
	}
	inertHolds.set(target, { count: 1, was: target.inert });
	target.inert = true;
}

export function releaseInert(target: Inertable): void {
	const hold = inertHolds.get(target);
	if (!hold) return;
	if (--hold.count > 0) return;
	inertHolds.delete(target);
	target.inert = hold.was;
}

type Connectable = { isConnected: boolean };

/** What a closing modal remembers about where focus came from. `via` is the
 * layer that held focus when this one opened (a prompt opened from the
 * palette), so a return target that has since left the document can fall back
 * along the chain. */
export interface ReturnChain<E extends Connectable> {
	node: E;
	returnTo: E | null;
	via?: ReturnChain<E>;
}

/** The element a closing modal should hand focus to: its own return target if
 * still in the document; else, when the layer it came from is still open, that
 * layer (focus must not leak behind it); else wherever that layer would have
 * returned to. */
export function pickReturnTarget<E extends Connectable>(
	layer: ReturnChain<E>,
	isOpen: (layer: ReturnChain<E>) => boolean
): E | null {
	for (let current: ReturnChain<E> | undefined = layer; current; current = current.via) {
		if (current.returnTo?.isConnected) return current.returnTo;
		const via = current.via;
		if (via && isOpen(via) && via.node.isConnected) return via.node;
	}
	return null;
}

export interface ModalOptions {
	/** First focus on open. Defaults to the first tabbable, then the node. */
	initialFocus?: () => HTMLElement | null | undefined;
	/** Escape, when this is the topmost modal. */
	onEscape?: () => void;
	/** Made inert while open. Only for dialogs that sit beside the app shell
	 * (UIHost's); anything rendered inside the shell would inert itself. */
	inert?: HTMLElement | null;
}

interface Layer extends ReturnChain<HTMLElement> {
	options: ModalOptions;
	via?: Layer;
}

const layers = new LayerStack<Layer>();

/** True while any modal is open. For Escape handlers on non-modal chrome (a
 * dropdown, a banner) that must leave the key to the modal on top of them. */
export function modalOpen(): boolean {
	return layers.size > 0;
}

/** The app shell behind layout-level dialogs (+layout's `.shell`), for `inert`. */
export function appShell(): HTMLElement | null {
	return document.querySelector<HTMLElement>('.shell');
}

function tabbables(node: HTMLElement): HTMLElement[] {
	// No client rects means display:none (the palette's phone-only close
	// button on desktop) — focus() would silently do nothing there.
	return Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
		(element) => element.getClientRects().length > 0
	);
}

function onWindowKeydown(event: KeyboardEvent) {
	// Bubble phase, so a control inside the modal that claims the key (a
	// context menu's Escape) keeps it by preventing the default.
	if (event.defaultPrevented) return;
	const top = layers.top();
	if (!top) return;
	if (event.key === 'Escape' && !event.isComposing) {
		event.preventDefault();
		top.options.onEscape?.();
	} else if (event.key === 'Tab') {
		const list = tabbables(top.node);
		const target = list.length ? nextFocusTarget(list, document.activeElement, event.shiftKey) : top.node;
		if (!target) return;
		event.preventDefault();
		target.focus();
	}
}

export function modal(
	node: HTMLElement,
	options: ModalOptions = {}
): ActionReturn<ModalOptions> & { destroy(): void } {
	const active = document.activeElement;
	const layer: Layer = {
		node,
		options,
		returnTo: active instanceof HTMLElement && active !== document.body ? active : null,
		via: active ? layers.find((open) => open.node.contains(active)) : undefined
	};
	const addedTabindex = !node.hasAttribute('tabindex');
	if (addedTabindex) node.tabIndex = -1;
	const inert = options.inert ?? null;
	if (inert) holdInert(inert);
	layers.push(layer);
	if (layers.size === 1) window.addEventListener('keydown', onWindowKeydown);

	let alive = true;
	// A microtask, not now: an action runs before the `bind:this` of the
	// children `initialFocus` usually returns.
	queueMicrotask(() => {
		if (!alive || node.contains(document.activeElement)) return;
		const target = layer.options.initialFocus?.() ?? tabbables(node)[0] ?? node;
		target.focus({ preventScroll: true });
	});

	return {
		update(next: ModalOptions) {
			layer.options = next;
		},
		destroy() {
			alive = false;
			layers.pop(layer);
			if (layers.size === 0) window.removeEventListener('keydown', onWindowKeydown);
			// Before focusing: nothing inside an inert tree can take focus.
			if (inert) releaseInert(inert);
			if (addedTabindex) node.removeAttribute('tabindex');
			// Only when focus was left in this modal or dropped to <body> by its
			// removal. Anything else took it on purpose — the next dialog, an
			// editor that focused itself on a freshly opened note.
			const now = document.activeElement;
			if (now && now !== document.body && !node.contains(now)) return;
			pickReturnTarget(layer, (open) => layers.has(open as Layer))?.focus({ preventScroll: true });
		}
	};
}
