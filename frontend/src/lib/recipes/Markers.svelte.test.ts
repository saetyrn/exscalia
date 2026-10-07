// @vitest-environment jsdom
import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Markers from './Markers.svelte';

let cleanup: (() => void) | undefined;

function render(props: ComponentProps<typeof Markers>) {
	const target = document.body.appendChild(document.createElement('div'));
	const component = mount(Markers, { target, props });
	flushSync();
	cleanup = () => {
		unmount(component);
		target.remove();
	};
	return target;
}

afterEach(() => {
	cleanup?.();
	cleanup = undefined;
});

describe('Markers', () => {
	it('names each marker in the recipe app’s words', () => {
		const el = render({ markers: ['estimated', 'rounded', 'to_taste'] });
		expect([...el.querySelectorAll('.marker')].map((m) => m.textContent)).toEqual([
			'estimated',
			'rounded',
			'starting point'
		]);
		// No confirm without a handler: nothing is a button.
		expect(el.querySelector('button')).toBeNull();
	});

	it('draws nothing for no markers', () => {
		const el = render({ markers: [] });
		expect(el.querySelector('.markers')).toBeNull();
	});

	it('makes "estimated" the confirm button when it can confirm', () => {
		const onconfirm = vi.fn();
		const el = render({ markers: ['estimated', 'rounded'], onconfirm });
		const buttons = el.querySelectorAll('button');
		expect(buttons).toHaveLength(1);
		expect(buttons[0].textContent).toBe('estimated');
		buttons[0].click();
		expect(onconfirm).toHaveBeenCalledTimes(1);
		// "rounded" is never a button.
		expect(el.querySelector('span.marker')?.textContent).toBe('rounded');
	});

	it('holds the confirm while one is saving', () => {
		const onconfirm = vi.fn();
		const el = render({ markers: ['estimated'], onconfirm, busy: true });
		const button = el.querySelector('button') as HTMLButtonElement;
		expect(button.disabled).toBe(true);
		button.click();
		expect(onconfirm).not.toHaveBeenCalled();
	});
});
