// The tab-focus escape hatch is what made Tab "work sometimes": Escape arms it,
// and nothing ever turned it back off.
import { describe, expect, it, vi } from 'vitest';
import { clearTabFocusOnEscape, tabFocusToggleBinding } from './tabmode';

function host() {
	return { setTabFocusMode: vi.fn() };
}

const key = (name: string) => ({ key: name }) as KeyboardEvent;

describe('clearTabFocusOnEscape', () => {
	it('releases tab-focus mode after Escape', async () => {
		const view = host();
		expect(clearTabFocusOnEscape(key('Escape'), view)).toBe(false);
		// Deferred on purpose: CodeMirror's own handler arms the two-second
		// window after this one runs, so clearing has to land after it.
		expect(view.setTabFocusMode).not.toHaveBeenCalled();
		await Promise.resolve();
		expect(view.setTabFocusMode).toHaveBeenCalledWith(false);
	});

	it('ignores every other key', async () => {
		const view = host();
		expect(clearTabFocusOnEscape(key('Tab'), view)).toBe(false);
		expect(clearTabFocusOnEscape(key('a'), view)).toBe(false);
		await Promise.resolve();
		expect(view.setTabFocusMode).not.toHaveBeenCalled();
	});

	it('never consumes Escape — the palette and toolbars still need it', () => {
		expect(clearTabFocusOnEscape(key('Escape'), host())).toBe(false);
	});
});

describe('tabFocusToggleBinding', () => {
	it('covers both of defaultKeymap\'s toggleTabFocusMode keys', () => {
		expect(tabFocusToggleBinding.key).toBe('Ctrl-m');
		expect(tabFocusToggleBinding.mac).toBe('Shift-Alt-m');
	});

	it('swallows the key so the toggle underneath is unreachable', () => {
		expect(tabFocusToggleBinding.run?.(null as never)).toBe(true);
	});
});
