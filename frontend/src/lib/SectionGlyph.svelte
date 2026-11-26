<!--
	The sidebar's section glyphs. Each one plays a short motion while its tile
	(an ancestor with the class `section-tile`) is hovered or keyboard focused.
	The tile itself never moves: only the strokes inside the glyph do, so the
	no-movement hover rule still holds for the button. Same 24px box, round
	caps and joins as the Glyph registry; still under reduced motion.
-->
<script lang="ts" module>
	export type SectionGlyphName =
		| 'omnia'
		| 'calendar'
		| 'tasks'
		| 'media'
		| 'recipes'
		| 'gallery'
		| 'paper'
		| 'daily';
</script>

<script lang="ts">
	let { name, size = 22 }: { name: SectionGlyphName; size?: number } = $props();
	const uid = $props.id();
</script>

<svg
	class="section-glyph"
	viewBox="0 0 24 24"
	width={size}
	height={size}
	aria-hidden="true"
	focusable="false"
>
	{#if name === 'omnia'}
		<!-- A speech bubble whose three dots type. -->
		<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z" />
		<circle class="fill hop" cx="8" cy="12" r="1.2" />
		<circle class="fill hop d1" cx="12" cy="12" r="1.2" />
		<circle class="fill hop d2" cx="16" cy="12" r="1.2" />
	{:else if name === 'calendar'}
		<!-- A marker walks across the days. -->
		<path d="M8 2.5v4M16 2.5v4" />
		<rect x="3" y="4.5" width="18" height="16.5" rx="2" />
		<path d="M3 9.5h18" />
		<path
			class="faint"
			d="M7 13.5h.01M12 13.5h.01M17 13.5h.01M7 18h.01M12 18h.01M17 18h.01"
			stroke-width="2.4"
		/>
		<g class="walk"><rect class="fill" x="5.5" y="12" width="3" height="3" rx=".8" /></g>
	{:else if name === 'tasks'}
		<!-- Three checks tick down a list. -->
		<path d="M11 6h10M11 12h10M11 18h10" />
		<path class="draw tick" pathLength="1" d="M3 6l1.6 1.6L7.5 4.5" />
		<path class="draw tick d1" pathLength="1" d="M3 12l1.6 1.6 2.9-3.1" />
		<path class="draw tick d2" pathLength="1" d="M3 18l1.6 1.6 2.9-3.1" />
	{:else if name === 'media'}
		<!-- A clapperboard snaps shut. -->
		<path d="M4 10.5h16v8.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" />
		<g class="clap">
			<path d="M4 10.5 3.3 7.3l15.6-3.6.7 3.2z" />
			<path d="M8 6.4l1.9 2.6M12.5 5.4l1.9 2.6" />
		</g>
	{:else if name === 'recipes'}
		<!-- Fork and knife tip toward each other. -->
		<g class="tilt-l"><path d="M6 3v6a2 2 0 0 0 2 2 2 2 0 0 0 2-2V3M8 3v18" /></g>
		<g class="tilt-r"><path d="M17.5 21V3c-2.2 1.2-3.5 4.5-3.5 9h3.5" /></g>
	{:else if name === 'gallery'}
		<!-- A sun rises from behind the mountains in a frame. -->
		<defs>
			<clipPath id="{uid}-sky"><path d="M3 4h18v10L15 13l-3 3-4-4-5 5z" /></clipPath>
		</defs>
		<rect x="3" y="4" width="18" height="16" rx="2" />
		<g clip-path="url(#{uid}-sky)"><circle class="rise" cx="15.5" cy="9" r="2" /></g>
		<path d="M3 17l5-5 4 4 3-3 6 6" />
	{:else if name === 'paper'}
		<!-- A pencil writes a line. -->
		<path
			class="draw scribble"
			pathLength="1"
			d="M3 21c1.8-1.6 2.8 1.6 4.6 0s2.8 1.6 4.6 0 2.8 1.6 4.6 0"
		/>
		<g class="glide"><path d="M11.5 2.5l2.5 2.5L6 13l-3.2.7L3.5 10.5z" /></g>
	{:else if name === 'daily'}
		<!-- The sun comes up over the horizon. -->
		<defs>
			<clipPath id="{uid}-horizon"><rect x="0" y="0" width="24" height="18" /></clipPath>
		</defs>
		<g clip-path="url(#{uid}-horizon)"><g class="sunup"><path d="M7 18a5 5 0 0 1 10 0" /></g></g>
		<path d="M2 18h20M6 21.5h12" />
		<g class="rays"><path d="M12 6v3M5.6 10.6l1.8 1.8M18.4 10.6l-1.8 1.8" /></g>
	{/if}
</svg>

<style>
	.section-glyph {
		flex-shrink: 0;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.8;
		stroke-linecap: round;
		stroke-linejoin: round;
		overflow: visible;
	}
	.section-glyph * {
		transform-box: fill-box;
		transform-origin: center;
	}
	.fill {
		fill: currentColor;
		stroke: none;
	}
	.faint {
		opacity: 0.3;
	}
	.draw {
		stroke-dasharray: 1;
		stroke-dashoffset: 0;
	}

	@keyframes hop {
		0%,
		100% {
			transform: none;
		}
		50% {
			transform: translateY(-2.5px);
		}
	}
	@keyframes walk {
		0% {
			transform: translate(0, 0);
		}
		20% {
			transform: translate(5px, 0);
		}
		40% {
			transform: translate(10px, 0);
		}
		60% {
			transform: translate(0, 4.5px);
		}
		80% {
			transform: translate(5px, 4.5px);
		}
		100% {
			transform: translate(10px, 4.5px);
		}
	}
	@keyframes draw-in {
		from {
			stroke-dashoffset: 1;
		}
		to {
			stroke-dashoffset: 0;
		}
	}
	@keyframes clap {
		0%,
		100% {
			transform: rotate(0);
		}
		35% {
			transform: rotate(-22deg);
		}
		55% {
			transform: rotate(0);
		}
		65% {
			transform: rotate(-4deg);
		}
		75% {
			transform: rotate(0);
		}
	}
	@keyframes tilt-l {
		0%,
		100% {
			transform: rotate(0);
		}
		50% {
			transform: rotate(12deg);
		}
	}
	@keyframes tilt-r {
		0%,
		100% {
			transform: rotate(0);
		}
		50% {
			transform: rotate(-12deg);
		}
	}
	@keyframes rise {
		from {
			transform: translateY(5px);
			opacity: 0.15;
		}
		to {
			transform: none;
			opacity: 1;
		}
	}
	@keyframes glide {
		from {
			transform: translateX(0);
		}
		to {
			transform: translateX(11px);
		}
	}
	@keyframes sunup {
		from {
			transform: translateY(7px);
		}
		to {
			transform: none;
		}
	}
	@keyframes rays-in {
		0%,
		40% {
			opacity: 0;
		}
		100% {
			opacity: 1;
		}
	}

	:global(.section-tile:is(:hover, :focus-visible)) .hop {
		animation: hop 0.9s ease-in-out infinite;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .walk {
		animation: walk 1.2s steps(1) infinite;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .tick {
		animation: draw-in 0.35s ease-out both;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .clap {
		animation: clap 0.6s ease-in-out;
		transform-origin: 0% 100%;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .tilt-l {
		animation: tilt-l 0.7s ease-in-out;
		transform-origin: 50% 100%;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .tilt-r {
		animation: tilt-r 0.7s ease-in-out;
		transform-origin: 50% 100%;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .rise {
		animation: rise 0.7s ease-out both;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .scribble {
		animation: draw-in 1s ease-in-out both;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .glide {
		animation: glide 1s ease-in-out both;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .sunup {
		animation: sunup 0.8s ease-out both;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .rays {
		animation: rays-in 0.9s ease-out both;
	}
	/* Staggers: the second and third dot or check start a beat later. */
	:global(.section-tile:is(:hover, :focus-visible)) .hop.d1 {
		animation-delay: 0.15s;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .hop.d2 {
		animation-delay: 0.3s;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .tick.d1 {
		animation-delay: 0.3s;
	}
	:global(.section-tile:is(:hover, :focus-visible)) .tick.d2 {
		animation-delay: 0.6s;
	}

	@media (prefers-reduced-motion: reduce) {
		.section-glyph * {
			animation: none !important;
		}
	}
</style>
