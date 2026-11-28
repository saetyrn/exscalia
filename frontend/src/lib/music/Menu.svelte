<!--
	A small popup menu at a point (right-click on desktop, long press on a
	phone). Items run and close it; Escape and a click outside close it too.
-->
<script lang="ts" module>
	export interface MenuItem {
		label: string;
		run: () => void;
		danger?: boolean;
		disabled?: boolean;
	}
</script>

<script lang="ts">
	import { modal } from '$lib/modal';

	let {
		x,
		y,
		items,
		onclose
	}: { x: number; y: number; items: MenuItem[]; onclose: () => void } = $props();

	let node: HTMLElement | undefined = $state();
	let left = $state(0);
	let top = $state(0);

	$effect(() => {
		if (!node) return;
		const rect = node.getBoundingClientRect();
		left = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8));
		top = Math.max(8, Math.min(y, window.innerHeight - rect.height - 8));
	});

	function pick(item: MenuItem) {
		if (item.disabled) return;
		const run = item.run;
		onclose();
		run();
	}
</script>

<div class="scrim" role="presentation" onpointerdown={onclose} oncontextmenu={(e) => { e.preventDefault(); onclose(); }}></div>
<div
	class="menu"
	role="menu"
	tabindex="-1"
	bind:this={node}
	style:left="{left}px"
	style:top="{top}px"
	use:modal={{ onEscape: onclose }}
>
	{#each items as item (item.label)}
		<button
			type="button"
			role="menuitem"
			class:danger={item.danger}
			disabled={item.disabled}
			onclick={() => pick(item)}>{item.label}</button
		>
	{/each}
</div>

<style>
	.scrim {
		position: fixed;
		inset: 0;
		z-index: var(--z-palette);
	}
	.menu {
		position: fixed;
		z-index: var(--z-palette);
		min-width: 200px;
		padding: 5px;
		display: flex;
		flex-direction: column;
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: 10px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18);
	}
	button {
		padding: 8px 12px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--text);
		font: inherit;
		font-size: 14px;
		text-align: left;
		cursor: pointer;
	}
	button:hover,
	button:focus-visible {
		background: var(--bg-hover);
		outline: none;
	}
	button.danger {
		color: var(--danger);
	}
	button:disabled {
		opacity: 0.45;
		cursor: default;
	}
	@media (max-width: 760px) {
		button {
			min-height: 44px;
			font-size: var(--m-row-text);
		}
	}
</style>
