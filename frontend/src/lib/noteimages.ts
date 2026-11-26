// Images in notes, outside the editor: their bytes, the clipboard, a download.
//
// The note keeps an image as `![alt](/api/attachments/…)` markdown, which the
// formatted view never shows. Copying one has to put a real picture on the
// clipboard, so it pastes into other apps; pasting that picture back into a
// note should bring back the same attachment rather than upload a duplicate.

import { apiFetch, LONG_TIMEOUT_MS } from './api';

/** An attachment's bytes. Through `apiFetch` for the app's own URLs, so the
 *  desktop app's bearer header goes along. */
export async function imageBlob(url: string): Promise<Blob> {
	const res = url.startsWith('/api/')
		? await apiFetch(url, { timeoutMs: LONG_TIMEOUT_MS })
		: await fetch(url);
	if (!res.ok) throw new Error(`Could not load the image (${res.status}).`);
	return res.blob();
}

/** The same picture as PNG: the only image type every browser's clipboard
 *  takes. Drawn through an <img> rather than createImageBitmap, which some
 *  engines refuse for SVG. */
export async function asPng(blob: Blob): Promise<Blob> {
	if (blob.type === 'image/png') return blob;
	const objectUrl = URL.createObjectURL(blob);
	try {
		const img = new Image();
		img.src = objectUrl;
		await img.decode();
		const canvas = document.createElement('canvas');
		canvas.width = img.naturalWidth || 1;
		canvas.height = img.naturalHeight || 1;
		canvas.getContext('2d')?.drawImage(img, 0, 0);
		return await new Promise<Blob>((resolve, reject) =>
			canvas.toBlob(
				(png) => (png ? resolve(png) : reject(new Error('Could not copy the image.'))),
				'image/png'
			)
		);
	} finally {
		URL.revokeObjectURL(objectUrl);
	}
}

/** The last picture this window put on the clipboard, and the markdown it
 *  came from. */
let lastCopied: { size: number; markdown: string } | null = null;

/** Put the picture itself on the clipboard.
 *
 *  The ClipboardItem is built synchronously with a promise for its bytes:
 *  Safari and the desktop app's WebKit only accept a write that starts inside
 *  the click or key press, and the download comes after. */
export function copyImage(url: string, markdown: string): Promise<void> {
	if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) {
		return Promise.reject(new Error('This browser cannot copy images.'));
	}
	const png = imageBlob(url)
		.then(asPng)
		.then((blob) => {
			lastCopied = { size: blob.size, markdown };
			return blob;
		});
	return navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
}

/** The markdown to paste for these files, when they are exactly the picture
 *  this window copied last; null means upload them. */
export function pastedCopy(files: readonly File[]): string | null {
	if (!lastCopied || files.length !== 1) return null;
	const [file] = files;
	return file.type === 'image/png' && file.size === lastCopied.size ? lastCopied.markdown : null;
}

/** A file name for a downloaded image: its alt text when that reads as one,
 *  with the extension its bytes call for. */
export function downloadName(alt: string, url: string, type: string): string {
	const fromUrl = decodeURIComponent(url.split(/[?#]/)[0].split('/').pop() ?? '');
	const ext = type.split('/')[1]?.replace('jpeg', 'jpg').replace(/\+.*$/, '') || fromUrl.split('.').pop() || 'png';
	const base = (alt.trim() || fromUrl || 'image').replace(/[\\/:*?"<>|]+/g, '-').replace(/\.[a-z0-9]{2,5}$/i, '');
	return `${base || 'image'}.${ext}`;
}

/** Save the image to the user's downloads. */
export async function downloadImage(url: string, alt: string): Promise<void> {
	const blob = await imageBlob(url);
	const objectUrl = URL.createObjectURL(blob);
	const link = document.createElement('a');
	link.href = objectUrl;
	link.download = downloadName(alt, url, blob.type);
	document.body.appendChild(link);
	link.click();
	link.remove();
	// The click has handed the URL to the browser's download by now.
	setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000);
}
