<script lang="ts">
	// The recipes display type: Playfair Display stretched tall, after the
	// recipe app's StretchedTitle. Sized to fit its column on one line where it
	// can; long titles wrap at `minSize`. The box is measured so the stretch
	// takes real space in the layout (a scaleY alone would overlap what
	// follows), before paint, so a sliding page never shows it at the wrong
	// height.
	let {
		text,
		stretch = 2.2,
		minSize = 30,
		maxSize = 84,
		as = 'h1',
		align = 'left',
		class: className = ''
	}: {
		text: string;
		/** Vertical stretch, as on the menu. */
		stretch?: number;
		/** Font size bounds in px. */
		minSize?: number;
		maxSize?: number;
		as?: 'h1' | 'h2' | 'p';
		/** Which edge the stretch grows from. */
		align?: 'left' | 'center';
		class?: string;
	} = $props();

	let outer = $state<HTMLElement>();
	let inner = $state<HTMLSpanElement>();
	let width = $state(0);
	let innerHeight = $state(0);

	let size = $derived(
		width
			? Math.min(maxSize, Math.max(minSize, width / (Math.max(6, text.trim().length) * 0.47)))
			: minSize
	);

	$effect(() => {
		const o = outer;
		const i = inner;
		if (!o || !i) return;
		const measure = () => {
			width = o.clientWidth;
			innerHeight = i.offsetHeight;
		};
		measure();
		const ro = new ResizeObserver(measure);
		ro.observe(o);
		ro.observe(i);
		return () => ro.disconnect();
	});
</script>

<svelte:element
	this={as}
	bind:this={outer}
	class="rc-stretched {className}"
	class:center={align === 'center'}
	style:height={innerHeight ? `${innerHeight * stretch}px` : undefined}
	aria-label={text}
>
	<span
		bind:this={inner}
		aria-hidden="true"
		style:font-size="{size}px"
		style:transform="scaleY({stretch})">{text}</span
	>
</svelte:element>

<style>
	.rc-stretched {
		margin: 0;
		font-weight: 400;
	}
	.rc-stretched span {
		display: block;
		font-family: var(--rc-serif, serif);
		font-weight: 400;
		letter-spacing: -0.05em;
		line-height: 1.1;
		color: var(--rc-ink, inherit);
		transform-origin: top left;
		text-shadow: var(--rc-fringe, none);
		overflow-wrap: anywhere;
	}
	.rc-stretched.center {
		text-align: center;
	}
	.rc-stretched.center span {
		transform-origin: top center;
	}
</style>
