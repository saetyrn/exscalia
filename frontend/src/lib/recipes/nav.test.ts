import { beforeEach, describe, expect, it, vi } from 'vitest';

const goto = vi.fn(async (_href: string, _opts?: unknown) => {});
const page = { state: {} as Record<string, unknown> };
vi.mock('$app/navigation', () => ({ goto }));
vi.mock('$app/state', () => ({ page }));
const requestSlide = vi.fn();
const SUPERSEDED = new Error('superseded');
vi.mock('./transition', () => ({ requestSlide, isSuperseded: (e: unknown) => e === SUPERSEDED }));

const { back, forward, slideTo } = await import('./nav');

describe('slide navigation', () => {
	beforeEach(() => {
		goto.mockClear();
		requestSlide.mockClear();
		page.state = {};
	});

	it('asks for the goto it makes to slide', async () => {
		await forward('/recipes/recipes/a.md');
		expect(requestSlide).toHaveBeenCalledWith('forward', '/recipes/recipes/a.md');
		expect(goto).toHaveBeenCalledWith('/recipes/recipes/a.md', {
			replaceState: false,
			state: { recipesBack: true }
		});
		await slideTo('/recipes', 'back', { replace: true });
		expect(requestSlide).toHaveBeenLastCalledWith('back', '/recipes');
	});

	it('goes back through history when the page was slid into', async () => {
		const historyBack = vi.fn();
		vi.stubGlobal('history', { back: historyBack });
		page.state = { recipesBack: true };
		await back('/recipes');
		expect(requestSlide).toHaveBeenCalledWith('back', null);
		expect(historyBack).toHaveBeenCalledOnce();
		expect(goto).not.toHaveBeenCalled();
		vi.unstubAllGlobals();
	});

	it('settles quietly when a newer navigation supersedes the slide', async () => {
		goto.mockRejectedValueOnce(SUPERSEDED);
		await expect(forward('/recipes/recipes/a.md')).resolves.toBeUndefined();
		const failure = new Error('Not Found');
		goto.mockRejectedValueOnce(failure);
		await expect(slideTo('/recipes', 'back')).rejects.toBe(failure);
	});

	it('otherwise slides back to the fallback', async () => {
		await back('/recipes');
		expect(requestSlide).toHaveBeenCalledWith('back', '/recipes');
		expect(goto).toHaveBeenCalledWith('/recipes');
	});
});
