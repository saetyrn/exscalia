import { describe, expect, it } from 'vitest';
import { imageFiles, pictureCount, shortAge } from './format';

describe('gallery words', () => {
	const now = Date.parse('2026-11-08T12:00:00Z');
	it('says how long ago, as Pinterest does', () => {
		expect(shortAge('2026-11-08T11:59:30Z', now)).toBe('now');
		expect(shortAge('2026-11-05T12:00:00Z', now)).toBe('3d');
		expect(shortAge('2026-10-11T12:00:00Z', now)).toBe('4w');
		expect(shortAge('2026-05-08T12:00:00Z', now)).toBe('6mo');
		expect(shortAge(null, now)).toBe('');
	});
	it('counts pictures', () => {
		expect(pictureCount(1)).toBe('1 picture');
		expect(pictureCount(85)).toBe('85 pictures');
		expect(pictureCount(2300)).toBe('2.3k pictures');
	});
	it('keeps only the image types the server takes', () => {
		const file = (type: string) => ({ type }) as File;
		expect(
			imageFiles([
				file('image/png'),
				file('text/plain'),
				file('image/heic'),
				file('image/webp')
			]).map((f) => f.type)
		).toEqual(['image/png', 'image/webp']);
	});
});
