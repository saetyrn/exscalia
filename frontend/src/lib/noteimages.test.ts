import { describe, expect, it } from 'vitest';
import { downloadName } from './noteimages';

describe('downloadName', () => {
	it('names the file after the alt text, with the extension its bytes call for', () => {
		expect(downloadName('Kitchen plan', '/api/attachments/3f2a.png', 'image/png')).toBe('Kitchen plan.png');
		expect(downloadName('photo.JPG', '/api/attachments/3f2a.jpg', 'image/jpeg')).toBe('photo.jpg');
		expect(downloadName('logo', '/api/attachments/9c.svg', 'image/svg+xml')).toBe('logo.svg');
	});

	it('falls back to the attachment name, then to "image"', () => {
		expect(downloadName('  ', '/api/attachments/3f2a.webp?r=2', 'image/webp')).toBe('3f2a.webp');
		expect(downloadName('', '', '')).toBe('image.png');
	});

	it('keeps characters a file system refuses out of the name', () => {
		expect(downloadName('a/b:c?', '/x.png', 'image/png')).toBe('a-b-c-.png');
	});
});
