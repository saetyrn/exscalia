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
	// Start a new conversation — a sheet with a pencil across its corner, the
	// gesture iOS puts at the top-right of a message list.
	compose:
		'M20 13.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5.6M18.4 2.6a2 2 0 0 1 2.83 2.83L12.4 14.26 8.9 15.1l.84-3.5z',
	// A sheet with the corner turned down, plus the two lines of writing that
	// tell it apart from the plain rectangles above at 22px.
	// Paper: a pen nib — the section is handwriting, and a plain pencil would
	// read as "edit" beside the note glyph.
	paper: 'M8 3h8l1.5 8.5L12 22 6.5 11.5zM12 22v-8.6M12 10.4a1.4 1.4 0 1 1 0 2.8 1.4 1.4 0 0 1 0-2.8',
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
		'M3.2 17.4V6.9a1.8 1.8 0 0 1 1.8-1.8h3.4a1.8 1.8 0 0 1 1.45.73l.92 1.24a1.8 1.8 0 0 0 1.45.73h5.75a1.8 1.8 0 0 1 1.8 1.8v7.8a1.8 1.8 0 0 1-1.8 1.8H5a1.8 1.8 0 0 1-1.8-1.8Z',
	// Recipes. The section is a chef's hat; the pages' corner row uses the bare
	// chevron for back (the recipe app's own), the rest are their actions.
	recipe:
		'M17 21a1 1 0 0 0 1-1v-5.35c0-.46.32-.84.73-1.04a4 4 0 0 0-2.14-7.59 5 5 0 0 0-9.18 0 4 4 0 0 0-2.14 7.59c.41.2.73.58.73 1.04V20a1 1 0 0 0 1 1zM6 17h12',
	'chevron-left': 'M15 5l-7 7 7 7',
	edit: 'M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5zM15 5l4 4',
	redraw: 'M23 4v6h-6M20.49 15a9 9 0 1 1-2.12-9.36L23 10',
	trash:
		'M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M10 11v6M14 11v6',
	paperclip:
		'M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48',
	camera:
		'M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
	// A page with its corner turned and no writing: an uploaded file (a PDF),
	// where `note` is a note of the vault's own.
	file: 'M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM13 2v7h7',
	// Three lines narrowing downward: the Paper library's filter.
	filter: 'M4 6h16M7 12h10M10 18h4',
	// Two sheets, the front one offset: copy to the clipboard.
	copy: 'M9 9h11a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V10a1 1 0 0 1 1-1zM5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1',
	// Opposite corners pulled outward: show at full size.
	expand: 'M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7',
	download: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3',
	'arrow-up': 'M12 19V5M5 12l7-7 7 7',
	'arrow-down': 'M12 5v14M19 12l-7 7-7-7'
};
