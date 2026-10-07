import { describe, expect, it } from 'vitest';
import { dataUrlFile, isPdf, isUpload, MAX_FILES, MAX_PDF_BYTES, pickFiles, prepareUpload } from './files';

const file = (name: string, type: string, size = 10) => new File([new Uint8Array(size)], name, { type });

describe('add box files', () => {
	it('takes photos and PDFs, by type or by a .pdf name', () => {
		expect(isUpload(file('a.jpg', 'image/jpeg'))).toBe(true);
		expect(isUpload(file('scan.PDF', ''))).toBe(true);
		expect(isPdf(file('scan.pdf', ''))).toBe(true);
		expect(isUpload(file('clip.mp4', 'video/mp4'))).toBe(false);
		expect(isUpload(file('notes.txt', 'text/plain'))).toBe(false);
	});

	it('drops what it cannot read and says so', () => {
		const { take, error } = pickFiles([file('a.jpg', 'image/jpeg'), file('v.mp4', 'video/mp4')], 0);
		expect(take.map((f) => f.name)).toEqual(['a.jpg']);
		expect(error).toBe('Only photos and PDFs can be read.');
		expect(pickFiles([file('a.png', 'image/png')], 0).error).toBe('');
	});

	it('stops at six files for one recipe', () => {
		const photos = Array.from({ length: 4 }, (_, i) => file(`${i}.jpg`, 'image/jpeg'));
		const { take, error } = pickFiles(photos, 4);
		expect(MAX_FILES).toBe(6);
		expect(take).toHaveLength(2);
		expect(error).toBe('Up to 6 files for one recipe.');
		expect(pickFiles(photos, 6).take).toEqual([]);
	});

	it('sends a PDF as it is, up to the document limit', async () => {
		const pdf = await prepareUpload(file('menu.pdf', 'application/pdf'), 1);
		expect(pdf).toMatchObject({ key: 1, kind: 'pdf', name: 'menu.pdf', thumb: null });
		expect(pdf.file.type).toBe('application/pdf');
		const untyped = await prepareUpload(file('menu.pdf', ''), 2);
		expect(untyped.file.type).toBe('application/pdf');
		await expect(prepareUpload(file('big.pdf', 'application/pdf', MAX_PDF_BYTES + 1), 3)).rejects.toThrow(
			'“big.pdf” is larger than 8 MB.'
		);
		await expect(prepareUpload(file('a.txt', 'text/plain'), 4)).rejects.toThrow('is not a photo or a PDF');
	});

	it('turns a data URL back into a file for the upload', async () => {
		const out = dataUrlFile('data:image/jpeg;base64,/9j/AA==', 'recipe.jpg');
		expect(out.name).toBe('recipe.jpg');
		expect(out.type).toBe('image/jpeg');
		expect([...new Uint8Array(await out.arrayBuffer())]).toEqual([0xff, 0xd8, 0xff, 0x00]);
	});
});
