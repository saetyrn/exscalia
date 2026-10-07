// The add box's import flows against stubbed client calls: typed text and a
// lone link to recipesImport, photos and PDFs to recipesImportFiles, the AI
// gate on files, the server's detail shown as it is, and the draft handed to
// the review page with the first photo for the card.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '$lib/api';
import type { Draft } from '../types';
import { emptyRecipe } from '../types';
import { AddBox } from './addbox.svelte';
import { clearDraft, readDraft } from './handoff.svelte';

const stub = vi.hoisted(() => ({
	recipesStatus: vi.fn(),
	recipesImport: vi.fn(),
	recipesImportFiles: vi.fn()
}));

vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, ...stub } };
});

// Photos are shrunk on a canvas, which node lacks: stand in a ready upload.
vi.mock('./files', async (importOriginal) => {
	const actual = await importOriginal<typeof import('./files')>();
	return {
		...actual,
		prepareUpload: vi.fn(async (file: File, key: number) =>
			actual.isPdf(file)
				? { key, kind: 'pdf', name: file.name, file, thumb: null }
				: {
						key,
						kind: 'image',
						name: file.name,
						file: new File(['jpeg'], 'photo.jpg', { type: 'image/jpeg' }),
						thumb: `data:image/jpeg;base64,${key}`
					}
		)
	};
});

const storage = new Map<string, string>();
vi.stubGlobal('sessionStorage', {
	getItem: (key: string) => storage.get(key) ?? null,
	setItem: (key: string, value: string) => void storage.set(key, value),
	removeItem: (key: string) => void storage.delete(key)
});

const draft = (title = 'Soup', method: Draft['method'] = 'local'): Draft => ({
	recipe: { ...emptyRecipe(), title },
	missing: ['No cooking time'],
	method
});

const photo = (name = 'page.jpg') => new File(['x'], name, { type: 'image/jpeg' });
const pdf = (name = 'card.pdf') => new File(['%PDF'], name, { type: 'application/pdf' });

beforeEach(() => {
	vi.clearAllMocks();
	storage.clear();
	clearDraft();
	stub.recipesStatus.mockResolvedValue({ ai: true });
});

describe('the add box', () => {
	it('reads typed text and hands the draft on', async () => {
		stub.recipesImport.mockResolvedValue(draft());
		const box = new AddBox();
		expect(box.ready).toBe(false);
		box.text = '  Soup\nServes 2\n\n1 leek  ';
		expect(box.ready).toBe(true);
		expect(await box.submit()).toEqual(draft());
		expect(stub.recipesImport).toHaveBeenCalledWith({ text: 'Soup\nServes 2\n\n1 leek' });
		expect(readDraft()).toEqual({ draft: draft(), thumb: null, thumbUrl: null });
		expect(box.busy).toBe(false);
	});

	it('sends a lone link as a link', async () => {
		stub.recipesImport.mockResolvedValue(draft('Pie', 'schema_org'));
		const box = new AddBox();
		box.text = ' https://example.com/pie ';
		await box.submit();
		expect(stub.recipesImport).toHaveBeenCalledWith({ url: 'https://example.com/pie' });
		box.text = 'https://example.com/pie and more';
		await box.submit();
		expect(stub.recipesImport).toHaveBeenLastCalledWith({ text: 'https://example.com/pie and more' });
	});

	it('shows the server’s detail as it is and keeps the box', async () => {
		stub.recipesImport.mockRejectedValue(new ApiError(422, 'That site does not allow reading its pages.'));
		const box = new AddBox();
		box.text = 'https://example.com/locked';
		expect(await box.submit()).toBeNull();
		expect(box.error).toBe('That site does not allow reading its pages.');
		expect(box.text).toBe('https://example.com/locked');
		expect(readDraft()).toBeNull();
	});

	it('reads photos and PDFs with the text, and keeps the first photo for the card', async () => {
		stub.recipesImportFiles.mockResolvedValue(draft('Cake', 'ai'));
		const box = new AddBox();
		await box.takeFiles([pdf(), photo('one.jpg'), photo('two.jpg')]);
		expect(box.uploads.map((u) => u.kind)).toEqual(['pdf', 'image', 'image']);
		box.text = ' page 2 is the icing ';
		await box.submit();
		const [files, text] = stub.recipesImportFiles.mock.calls[0];
		expect(files).toHaveLength(3);
		expect((files as File[]).map((f) => f.type)).toEqual(['application/pdf', 'image/jpeg', 'image/jpeg']);
		expect(text).toBe('page 2 is the icing');
		expect(readDraft()?.thumb).toBe(box.uploads[1].thumb);
		expect(stub.recipesImport).not.toHaveBeenCalled();
	});

	it('takes six files at most and says what it left', async () => {
		const box = new AddBox();
		await box.takeFiles(Array.from({ length: 4 }, (_, i) => photo(`${i}.jpg`)));
		await box.takeFiles([photo('4.jpg'), photo('5.jpg'), photo('6.jpg')]);
		expect(box.uploads).toHaveLength(6);
		expect(box.full).toBe(true);
		expect(box.error).toBe('Up to 6 files for one recipe.');
		box.remove(box.uploads[0].key);
		expect(box.uploads).toHaveLength(5);
		await box.takeFiles([new File(['v'], 'clip.mp4', { type: 'video/mp4' })]);
		expect(box.error).toBe('Only photos and PDFs can be read.');
		expect(box.uploads).toHaveLength(5);
	});

	it('takes no files when the AI step cannot run', async () => {
		stub.recipesStatus.mockResolvedValue({ ai: false });
		const box = new AddBox();
		await box.loadStatus();
		expect(box.ai).toBe(false);
		await box.takeFiles([photo()]);
		expect(box.uploads).toEqual([]);
		expect(box.error).toBe('Photos and PDFs need an AI provider.');
	});

	it('treats an unanswered status as no AI', async () => {
		stub.recipesStatus.mockRejectedValue(new ApiError(404, 'Not Found'));
		const box = new AddBox();
		await box.loadStatus();
		expect(box.ai).toBe(false);
		expect(stub.recipesStatus).toHaveBeenCalledTimes(1);
		await box.loadStatus();
		expect(stub.recipesStatus).toHaveBeenCalledTimes(1);
	});

	it('shows an AI error from a photo import', async () => {
		stub.recipesImportFiles.mockRejectedValue(new ApiError(502, 'The AI provider is not answering.'));
		const box = new AddBox();
		await box.takeFiles([photo()]);
		expect(await box.submit()).toBeNull();
		expect(box.error).toBe('The AI provider is not answering.');
		expect(box.uploads).toHaveLength(1);
	});
});
