// Pictures dropped anywhere on a Gallery page, or pasted while no field has
// the caret, go to the board on screen. The node gets `dropping` while files
// are dragged over it, for the page's outline.
import type { LiveNoteEvent } from '$lib/live';
import { modalOpen } from '$lib/modal';
import { imageFiles } from './format';

export interface DropOptions {
	onfiles: (files: File[]) => void;
}

function carriesFiles(event: DragEvent): boolean {
	return Array.from(event.dataTransfer?.types ?? []).includes('Files');
}

function typing(target: EventTarget | null): boolean {
	if (!(target instanceof HTMLElement)) return false;
	return target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);
}

export function pictureDrop(node: HTMLElement, options: DropOptions) {
	let current = options;
	let depth = 0;
	const clear = () => {
		depth = 0;
		node.classList.remove('dropping');
	};
	const enter = (event: DragEvent) => {
		if (!carriesFiles(event)) return;
		event.preventDefault();
		depth++;
		node.classList.add('dropping');
	};
	const over = (event: DragEvent) => {
		if (carriesFiles(event)) event.preventDefault();
	};
	const leave = () => {
		depth = Math.max(0, depth - 1);
		if (!depth) node.classList.remove('dropping');
	};
	const drop = (event: DragEvent) => {
		if (!carriesFiles(event)) return;
		event.preventDefault();
		clear();
		const files = imageFiles(event.dataTransfer?.files);
		if (files.length) current.onfiles(files);
	};
	const paste = (event: ClipboardEvent) => {
		if (modalOpen() || typing(event.target)) return;
		const files = imageFiles(event.clipboardData?.files);
		if (!files.length) return;
		event.preventDefault();
		current.onfiles(files);
	};
	node.addEventListener('dragenter', enter);
	node.addEventListener('dragover', over);
	node.addEventListener('dragleave', leave);
	node.addEventListener('drop', drop);
	window.addEventListener('paste', paste);
	return {
		update(next: DropOptions) {
			current = next;
		},
		destroy() {
			node.removeEventListener('dragenter', enter);
			node.removeEventListener('dragover', over);
			node.removeEventListener('dragleave', leave);
			node.removeEventListener('drop', drop);
			window.removeEventListener('paste', paste);
		}
	};
}

/** A live event about a Gallery note: what every Gallery list is made of. */
export const isGalleryEvent = (d: LiveNoteEvent): boolean =>
	(d.type === 'note' || d.type === 'note_removed' || d.type === 'note_moved') &&
	(!!d.path?.startsWith('gallery/') || !!d.new_path?.startsWith('gallery/'));
