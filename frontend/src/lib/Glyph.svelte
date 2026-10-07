<script lang="ts">
	import { ICONS } from '$lib/icons';

	// `filled` paints the outline solid — for glyphs that carry an on/off
	// state in their own shape (the watchlist bookmark), where a second icon
	// would only be the same path twice.
	let { name, filled = false }: { name: string; filled?: boolean } = $props();
</script>

<svg
	viewBox="0 0 24 24"
	fill={filled ? 'currentColor' : 'none'}
	stroke="currentColor"
	stroke-width="2"
	stroke-linecap="round"
	stroke-linejoin="round"
	aria-hidden="true"
>
	<path d={ICONS[name]} />
</svg>
