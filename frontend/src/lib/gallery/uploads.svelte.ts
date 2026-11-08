// Pictures dropped, pasted or picked go up one per request, a few at a
// time, so a drop of forty never builds one huge body and one bad file
// fails alone. The counts are reactive for the page's progress line.
import { api } from '$lib/api';
import type { Pin } from './types';

const AT_ONCE = 3;

export const uploads = $state({ total: 0, done: 0, failed: 0 });

export function uploading(): boolean {
	return uploads.done + uploads.failed < uploads.total;
}

/** Send `files` to `board`; `onSaved` hears each picture as it lands.
 * Resolves with the failures' messages once every file is through. */
export async function sendPictures(
	files: File[],
	board: string,
	onSaved: (pin: Pin, created: boolean) => void
): Promise<string[]> {
	if (!uploading()) {
		uploads.total = 0;
		uploads.done = 0;
		uploads.failed = 0;
	}
	uploads.total += files.length;
	const errors: string[] = [];
	const queue = [...files];
	async function worker() {
		for (let file = queue.shift(); file; file = queue.shift()) {
			try {
				const { pin, created } = await api.galleryUpload(file, board);
				uploads.done++;
				onSaved(pin, created);
			} catch (e) {
				uploads.failed++;
				errors.push(`${file.name}: ${e instanceof Error ? e.message : String(e)}`);
			}
		}
	}
	await Promise.all(Array.from({ length: Math.min(AT_ONCE, files.length) }, worker));
	return errors;
}
