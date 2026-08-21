/** Long-press drag for touch, as a Svelte action.
 *
 * HTML5 drag-and-drop never fires on touch, so every draggable surface in the
 * app was mouse-only. This is the one shared touch path: hold a still finger
 * on an element for a beat and it lifts into a ghost that follows the touch;
 * the consumer does its own hit-testing per move and commits on drop — the
 * same state machine its HTML5 handlers already drive.
 *
 * Scrolling stays natural: until the delay elapses the browser owns the
 * gesture (movement cancels the press), and only a live drag blocks it, via a
 * non-passive touchmove listener — `touch-action` can't be changed once a
 * gesture has started.
 */

export interface TouchDragCallbacks {
	/** The press matured: the drag is live. Set up drag state. */
	onStart: () => void;
	/** The finger moved; hit-test and update the drop preview. */
	onMove: (x: number, y: number) => void;
	/** Finger lifted mid-drag: commit. */
	onDrop: () => void;
	/** The system took the gesture back (or the element unmounted). */
	onCancel: () => void;
	/** Skip the gesture entirely (an open menu, a disabled row). */
	disabled?: () => boolean;
}

const PRESS_MS = 300;
const WIGGLE_PX = 10; // movement beyond this before the press matures = scroll

export function touchDrag(node: HTMLElement, callbacks: TouchDragCallbacks) {
	let timer: ReturnType<typeof setTimeout> | null = null;
	let startX = 0;
	let startY = 0;
	let pointerId: number | null = null;
	let dragging = false;
	let ghost: HTMLElement | null = null;

	const blockScroll = (event: TouchEvent) => {
		if (dragging) event.preventDefault();
	};
	const blockMenu = (event: Event) => {
		// Safari raises contextmenu on long-press; while a drag is live (or
		// about to be) it must not open the app's context menu on top.
		if (dragging || timer) {
			event.preventDefault();
			event.stopPropagation();
		}
	};

	function makeGhost(x: number, y: number) {
		const box = node.getBoundingClientRect();
		ghost = node.cloneNode(true) as HTMLElement;
		ghost.style.cssText =
			`position: fixed; left: 0; top: 0; width: ${box.width}px; ` +
			'margin: 0; z-index: 1000; pointer-events: none; opacity: 0.85; ' +
			'box-shadow: 0 8px 24px rgba(0,0,0,0.35); border-radius: 8px;';
		moveGhost(x, y);
		document.body.appendChild(ghost);
	}

	function moveGhost(x: number, y: number) {
		if (ghost) ghost.style.transform = `translate(${x - 20}px, ${y - 24}px)`;
	}

	function cleanup(cancelled: boolean) {
		if (timer) clearTimeout(timer);
		timer = null;
		if (dragging) {
			dragging = false;
			ghost?.remove();
			ghost = null;
			document.body.classList.remove('touch-dragging');
			if (cancelled) callbacks.onCancel();
		}
		pointerId = null;
	}

	function onPointerDown(event: PointerEvent) {
		if (event.pointerType !== 'touch' || !event.isPrimary) return;
		if (callbacks.disabled?.()) return;
		pointerId = event.pointerId;
		startX = event.clientX;
		startY = event.clientY;
		timer = setTimeout(() => {
			timer = null;
			dragging = true;
			navigator.vibrate?.(15);
			document.body.classList.add('touch-dragging');
			makeGhost(startX, startY);
			callbacks.onStart();
		}, PRESS_MS);
	}

	function onPointerMove(event: PointerEvent) {
		if (event.pointerId !== pointerId) return;
		if (!dragging) {
			// Still a maybe-scroll: real movement hands the gesture back.
			if (
				timer &&
				Math.hypot(event.clientX - startX, event.clientY - startY) > WIGGLE_PX
			) {
				clearTimeout(timer);
				timer = null;
				pointerId = null;
			}
			return;
		}
		moveGhost(event.clientX, event.clientY);
		callbacks.onMove(event.clientX, event.clientY);
	}

	function onPointerUp(event: PointerEvent) {
		if (event.pointerId !== pointerId) return;
		const wasDragging = dragging;
		cleanup(false);
		if (wasDragging) {
			// Draggable rows are often links or buttons; the click the browser
			// synthesizes right after this pointerup must not fire an action
			// on top of the drop.
			const swallow = (e: Event) => {
				e.preventDefault();
				e.stopPropagation();
			};
			node.addEventListener('click', swallow, { capture: true, once: true });
			setTimeout(() => node.removeEventListener('click', swallow, true), 400);
			callbacks.onDrop();
		}
	}

	function onPointerCancel(event: PointerEvent) {
		if (event.pointerId !== pointerId) return;
		cleanup(true);
	}

	node.addEventListener('pointerdown', onPointerDown);
	// Window-level: the finger leaves the element long before it stops moving.
	window.addEventListener('pointermove', onPointerMove);
	window.addEventListener('pointerup', onPointerUp);
	window.addEventListener('pointercancel', onPointerCancel);
	node.addEventListener('touchmove', blockScroll, { passive: false });
	node.addEventListener('contextmenu', blockMenu, true);

	return {
		destroy() {
			cleanup(true);
			node.removeEventListener('pointerdown', onPointerDown);
			window.removeEventListener('pointermove', onPointerMove);
			window.removeEventListener('pointerup', onPointerUp);
			window.removeEventListener('pointercancel', onPointerCancel);
			node.removeEventListener('touchmove', blockScroll);
			node.removeEventListener('contextmenu', blockMenu, true);
		}
	};
}
