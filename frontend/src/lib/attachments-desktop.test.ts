// Desktop image hydration fetches each /api/ image with the bearer header.
// The deadline covers the body, so a download gets the long allowance: at
// the 30 s default a photo on a slow link was cut off mid-body and shown as
// 'attachment unavailable'.
import { afterEach, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() =>
	vi.fn(async () => ({ ok: true, status: 200, blob: async () => new Blob(['x']) }))
);
vi.mock('./api', () => ({ apiFetch, LONG_TIMEOUT_MS: 300_000 }));

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

it('downloads images under the long deadline', async () => {
	const img = {
		getAttribute: (name: string) => (name === 'src' ? '/api/attachments/photo.jpg' : null),
		removeAttribute: vi.fn(),
		setAttribute: vi.fn(),
		src: ''
	};
	vi.stubGlobal('document', {
		documentElement: {},
		querySelectorAll: () => [img]
	});
	vi.stubGlobal(
		'MutationObserver',
		class {
			observe() {}
			disconnect() {}
		}
	);
	vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:photo');
	const { watchAttachmentImages } = await import('./attachments-desktop');

	watchAttachmentImages();
	await vi.waitFor(() => expect(img.src).toBe('blob:photo'));

	expect(apiFetch).toHaveBeenCalledWith('/api/attachments/photo.jpg', { timeoutMs: 300_000 });
});
