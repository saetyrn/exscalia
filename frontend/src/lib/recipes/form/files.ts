// Photos and PDFs for the add box, after the recipe app's files.ts. Photos
// are shrunk in the browser before they go (the model reads a 1600px image as
// well as a 12 MP one) and keep a small JPEG for the tile and, on Save, the
// recipe's card; PDFs go as they are.

export const MAX_FILES = 6;
/** The AI step's limit for one document (overseer/vision.py). */
export const MAX_PDF_BYTES = 8 * 1024 * 1024;
const PHOTO_MAX = 1600;
const PHOTO_QUALITY = 0.85;
const THUMB_MAX = 320;
const THUMB_QUALITY = 0.7;

/** A picked photo or PDF, ready to send. */
export interface Upload {
	key: number;
	kind: 'image' | 'pdf';
	name: string;
	/** What goes to the import route: the shrunk JPEG, or the PDF itself. */
	file: File;
	/** A small JPEG data URL of a photo: its tile, and the recipe's card. */
	thumb: string | null;
}

export const isPdf = (file: File): boolean =>
	file.type === 'application/pdf' || /\.pdf$/i.test(file.name);

export const isUpload = (file: File): boolean => file.type.startsWith('image/') || isPdf(file);

/** Which of `list` to take when `have` are already picked, and what to say
 * about the rest. */
export function pickFiles(list: readonly File[], have: number): { take: File[]; error: string } {
	const wanted = list.filter(isUpload);
	let error = wanted.length < list.length ? 'Only photos and PDFs can be read.' : '';
	const room = Math.max(0, MAX_FILES - have);
	if (wanted.length > room) error = `Up to ${MAX_FILES} files for one recipe.`;
	return { take: wanted.slice(0, room), error };
}

const nameOf = (file: File): string =>
	file.name || (file.type.startsWith('image/') ? 'Pasted image' : 'Document');

/** Draw a photo at most `max` px on its long side, as a JPEG canvas. */
async function draw(file: Blob, max: number): Promise<HTMLCanvasElement> {
	const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
	const scale = Math.min(1, max / Math.max(bitmap.width, bitmap.height));
	const canvas = document.createElement('canvas');
	canvas.width = Math.max(1, Math.round(bitmap.width * scale));
	canvas.height = Math.max(1, Math.round(bitmap.height * scale));
	canvas.getContext('2d')!.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
	bitmap.close();
	return canvas;
}

function toJpeg(canvas: HTMLCanvasElement, quality: number): Promise<Blob> {
	return new Promise((resolve, reject) =>
		canvas.toBlob(
			(blob) => (blob ? resolve(blob) : reject(new Error('The photo could not be read.'))),
			'image/jpeg',
			quality
		)
	);
}

/** Turn a picked, dropped or pasted file into an upload. Throws a message
 * fit to show when the file cannot be used. */
export async function prepareUpload(file: File, key: number): Promise<Upload> {
	const name = nameOf(file);
	if (isPdf(file)) {
		if (file.size > MAX_PDF_BYTES)
			throw new Error(`“${name}” is larger than ${MAX_PDF_BYTES / (1024 * 1024)} MB.`);
		const pdf = file.type === 'application/pdf' ? file : new File([file], name, { type: 'application/pdf' });
		return { key, kind: 'pdf', name, file: pdf, thumb: null };
	}
	if (!file.type.startsWith('image/')) throw new Error(`“${name}” is not a photo or a PDF.`);
	try {
		const [full, small] = await Promise.all([draw(file, PHOTO_MAX), draw(file, THUMB_MAX)]);
		const jpeg = await toJpeg(full, PHOTO_QUALITY);
		const stem = name.replace(/\.[^.]+$/, '') || 'photo';
		return {
			key,
			kind: 'image',
			name,
			file: new File([jpeg], `${stem}.jpg`, { type: 'image/jpeg' }),
			thumb: small.toDataURL('image/jpeg', THUMB_QUALITY)
		};
	} catch {
		throw new Error(`“${name}” could not be read.`);
	}
}

/** A data URL back to a file, for the attachment upload. */
export function dataUrlFile(dataUrl: string, name: string): File {
	const comma = dataUrl.indexOf(',');
	const type = /^data:([^;,]+)/.exec(dataUrl)?.[1] ?? 'application/octet-stream';
	const binary = atob(dataUrl.slice(comma + 1));
	const bytes = new Uint8Array(binary.length);
	for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
	return new File([bytes], name, { type });
}
