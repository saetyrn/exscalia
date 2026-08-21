// Monochrome stroke glyphs (feather/lucide outlines) — the design language
// is glyph + tooltip, never emoji. Rendered by Glyph.svelte.
//
// App-wide registry. It started under lib/media/ because /media was the only
// place drawing glyphs; the mobile tab bar needs the same vocabulary, so the
// module moved up to $lib rather than growing a second, drifting copy.
//
// Every path is drawn on a 24x24 box for stroke-width 2, round caps and
// joins — subpaths are separated with a fresh `M`, since Glyph renders them
// all through one <path>.
export const ICONS: Record<string, string> = {
	book: 'M2 4h6a3 3 0 0 1 3 3v12a2.5 2.5 0 0 0-2.5-2.5H2zM22 4h-6a3 3 0 0 0-3 3v12a2.5 2.5 0 0 1 2.5-2.5H22z',
	movie:
		'M7 3v18M17 3v18M2 12h20M2 8h5M2 16h5M17 16h5M17 8h5M4.2 3h15.6A2.2 2.2 0 0 1 22 5.2v13.6a2.2 2.2 0 0 1-2.2 2.2H4.2A2.2 2.2 0 0 1 2 18.8V5.2A2.2 2.2 0 0 1 4.2 3z',
	tv: 'M4 7h16a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2zM17 2l-5 5-5-5',
	game: 'M6 12h4m-2-2v4M15 11h.01M18 13h.01M17.32 5H6.68a4 4 0 0 0-3.978 3.59c-.006.052-.01.101-.017.152C2.604 9.416 2 14.456 2 16a3 3 0 0 0 3 3c1 0 1.5-.5 2-1l1.414-1.414A2 2 0 0 1 9.828 16h4.344a2 2 0 0 1 1.414.586L17 18c.5.5 1 1 2 1a3 3 0 0 0 3-3c0-1.545-.604-6.584-.685-7.258-.007-.05-.011-.1-.017-.151A4 4 0 0 0 17.32 5z',
	grid: 'M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z',
	rows: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
	plus: 'M12 5v14M5 12h14',
	x: 'M18 6L6 18M6 6l12 12',
	back: 'M19 12H5M12 19l-7-7 7-7',
	forward: 'M5 12h14M12 5l7 7-7 7',
	home: 'M3 10.5 12 3l9 7.5M5 9.5V20a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9.5',
	user: 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0z',
	image: 'M3 5h18a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zM8.5 11a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM21 15l-5-5L5 21',
	dots: 'M5 12h.01M12 12h.01M19 12h.01',
	// A sheet with the corner turned down, plus the two lines of writing that
	// tell it apart from the plain rectangles above at 22px.
	note: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M15 13H9M15 17H9',
	// OMNIA's speech bubble — the same outline the media hub's chat button
	// draws, so the tab and the button read as one affordance.
	chat: 'M7.9 20A9 9 0 1 0 4 16.1L2 22Z',
	// Mobile home tiles and the /notes browser.
	calendar: 'M5 4h14a1 1 0 0 1 1 1v15a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM4 9.5h16M8.5 2v4M15.5 2v4',
	// A ticked box: tasks, matching the sidebar's ☑ affordance in stroke form.
	task: 'M9 12.5l2.2 2.2L16 10M20 12.5V19a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9',
	search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20.5 20.5L16 16',
	// The media action bar. The star is the same outline StarRating draws, so
	// the Rate button and the stars it opens are one shape; the bookmark is
	// the watchlist, drawn hollow when off and filled (Glyph's `filled`) when
	// the item is on the list; the flag is the item's standing.
	star: 'M12 2l2.9 6.6 7.1.6-5.4 4.7 1.6 7-6.2-3.7-6.2 3.7 1.6-7L2 9.2l7.1-.6z',
	bookmark: 'M6 3h12a1 1 0 0 1 1 1v17l-7-4.6L5 21V4a1 1 0 0 1 1-1z',
	flag: 'M5 21V4M5 4h13l-2.2 4L18 12H5',
	check: 'M4 12.5l5 5L20 6.5',
	// Points right when closed; the /notes browser rotates it 90° when open,
	// which is orientation, not the movement the design rules forbid.
	chevron: 'M9 5l7 7-7 7',
	// The same outline FolderIcon.svelte draws, so a folder is one shape
	// across the app whichever component renders it.
	folder:
		'M3.2 17.4V6.9a1.8 1.8 0 0 1 1.8-1.8h3.4a1.8 1.8 0 0 1 1.45.73l.92 1.24a1.8 1.8 0 0 0 1.45.73h5.75a1.8 1.8 0 0 1 1.8 1.8v7.8a1.8 1.8 0 0 1-1.8 1.8H5a1.8 1.8 0 0 1-1.8-1.8Z'
};
