// Geometry for live drag previews.
//
// While animate:flip is sliding an element, getBoundingClientRect() reports
// the mid-animation position — so a gap computed from raw rects can flap: the
// preview reorders, rects glide past the pointer, the count changes, the
// preview reorders back. Subtracting the element's current translateY yields
// its settled layout position, which is where it will be when the animation
// lands — a fixed point the animation cannot move.

/** The element's vertical midpoint at its settled (untransformed) position. */
export function settledMidY(el: HTMLElement): number {
	const box = el.getBoundingClientRect();
	const transform = getComputedStyle(el).transform;
	const dy = transform && transform !== 'none' ? new DOMMatrixReadOnly(transform).m42 : 0;
	return box.top - dy + box.height / 2;
}

/** The element's horizontal midpoint at its settled (untransformed) position. */
export function settledMidX(el: HTMLElement): number {
	const box = el.getBoundingClientRect();
	const transform = getComputedStyle(el).transform;
	const dx = transform && transform !== 'none' ? new DOMMatrixReadOnly(transform).m41 : 0;
	return box.left - dx + box.width / 2;
}

/** How many of `elements` sit settled above `y` — the insertion gap index. */
export function gapIndexAmong(elements: Iterable<Element>, y: number): number {
	let k = 0;
	for (const el of elements) {
		if (y > settledMidY(el as HTMLElement)) k += 1;
	}
	return k;
}

/** How many of `elements` sit settled left of `x` — the insertion gap index. */
export function horizontalGapIndexAmong(elements: Iterable<Element>, x: number): number {
	let k = 0;
	for (const el of elements) {
		if (x > settledMidX(el as HTMLElement)) k += 1;
	}
	return k;
}
