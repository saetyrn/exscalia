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
 * gesture has started. In its place a live drag scrolls its own container
 * when the finger nears the top or bottom edge, so a target below the fold
 * is still reachable.
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
const EDGE_PX = 48; // a finger this close to the scroller's top/bottom scrolls it
const EDGE_MAX = 14; // px per frame with the finger at (or past) the edge

/** Edge autoscroll step in px per frame for a finger at `y` over a scroller
 *  spanning `rect`: 0 away from the edges, growing toward an edge up to
 *  `max`, negative (up) at the top. On a short scroller the zones shrink to
 *  half its height so they never overlap. */
export function edgeScrollSpeed(
	y: number,
	rect: { top: number; bottom: number },
	edge = EDGE_PX,
	max = EDGE_MAX
): number {
	const zone = Math.min(edge, (rect.bottom - rect.top) / 2);
	if (zone <= 0) return 0;
	const up = rect.top + zone - y;
	const down = y - (rect.bottom - zone);
	const depth = Math.max(up, down);
	if (depth <= 0) return 0;
	const speed = Math.ceil(max * Math.min(depth / zone, 1));
	return up > 0 ? -speed : speed;
}

/** The nearest ancestor that scrolls vertically and has something to
 *  scroll, else the page. */
export function scrollParent(el: Element): Element | null {
	for (let p = el.parentElement; p; p = p.parentElement) {
		const { overflowY } = getComputedStyle(p);
		if ((overflowY === 'auto' || overflowY === 'scroll') && p.scrollHeight > p.clientHeight) {
			return p;
		}
	}
	return document.scrollingElement;
}

export function touchDrag(node: HTMLElement, callbacks: TouchDragCallbacks) {
	let timer: ReturnType<typeof setTimeout> | null = null;
	let startX = 0;
	let startY = 0;
	let pointerId: number | null = null;
	let dragging = false;
	let ghost: HTMLElement | null = null;
	let scroller: Element | null = null;
	let frame: number | null = null;
	let lastX = 0;
	let lastY = 0;

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

	/** Autoscroll speed for the finger's last position; 0 when it is off to
	 *  the side of the scroller or away from its (on-screen) edges. */
	function edgeSpeed() {
		if (!scroller) return 0;
		const box = scroller.getBoundingClientRect();
		if (lastX < box.left || lastX > box.right) return 0;
		return edgeScrollSpeed(lastY, {
			top: Math.max(box.top, 0),
			bottom: Math.min(box.bottom, window.innerHeight)
		});
	}

	/** One autoscroll frame. The content moves under a still finger, and no
	 *  pointermove reports that, so the consumer re-hit-tests here. */
	function autoscroll() {
		frame = null;
		const speed = dragging ? edgeSpeed() : 0;
		if (!scroller || !speed) return;
		const before = scroller.scrollTop;
		// 'instant': the page itself scrolls smoothly (app.css), which would
		// turn every frame's step into a fresh animation.
		scroller.scrollBy({ top: speed, behavior: 'instant' });
		if (scroller.scrollTop === before) return; // already at the end
		callbacks.onMove(lastX, lastY);
		frame = requestAnimationFrame(autoscroll);
	}

	function cleanup(cancelled: boolean) {
		if (timer) clearTimeout(timer);
		timer = null;
		if (frame !== null) cancelAnimationFrame(frame);
		frame = null;
		scroller = null;
		window.removeEventListener('pointermove', onPointerMove);
		window.removeEventListener('pointerup', onPointerUp);
		window.removeEventListener('pointercancel', onPointerCancel);
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
		// Window-level, for this gesture only: the finger leaves the element
		// long before it stops moving.
		window.addEventListener('pointermove', onPointerMove);
		window.addEventListener('pointerup', onPointerUp);
		window.addEventListener('pointercancel', onPointerCancel);
		timer = setTimeout(() => {
			timer = null;
			dragging = true;
			scroller = scrollParent(node);
			lastX = startX;
			lastY = startY;
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
				cleanup(false);
			}
			return;
		}
		lastX = event.clientX;
		lastY = event.clientY;
		moveGhost(lastX, lastY);
		callbacks.onMove(lastX, lastY);
		if (frame === null && edgeSpeed() !== 0) frame = requestAnimationFrame(autoscroll);
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
	node.addEventListener('touchmove', blockScroll, { passive: false });
	node.addEventListener('contextmenu', blockMenu, true);

	return {
		destroy() {
			cleanup(true);
			node.removeEventListener('pointerdown', onPointerDown);
			node.removeEventListener('touchmove', blockScroll);
			node.removeEventListener('contextmenu', blockMenu, true);
		}
	};
}
