<script lang="ts">
	// The one drawn glyph in the sidebar: a plain folder. Everything else here
	// is a typographic mark, so this is stroked in currentColor at the same
	// weight as surrounding text and inherits the muted/hover colors without
	// any extra styling.
	let { size = 14 }: { size?: number } = $props();
</script>

<svg
	width={size}
	height={size}
	viewBox="0 0 24 24"
	fill="none"
	stroke="currentColor"
	stroke-width="1.9"
	stroke-linecap="round"
	stroke-linejoin="round"
	aria-hidden="true"
	focusable="false"
>
	<path
		d="M3.2 17.4V6.9a1.8 1.8 0 0 1 1.8-1.8h3.4a1.8 1.8 0 0 1 1.45.73l.92 1.24a1.8 1.8 0 0 0 1.45.73h5.75a1.8 1.8 0 0 1 1.8 1.8v7.8a1.8 1.8 0 0 1-1.8 1.8H5a1.8 1.8 0 0 1-1.8-1.8Z"
	/>
</svg>

<style>
	svg {
		display: block;
	}
</style>
