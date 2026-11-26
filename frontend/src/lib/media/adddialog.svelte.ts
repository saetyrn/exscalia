// Tiny cross-component state for the add-media dialog: the sidebar's +
// opens it from anywhere in media mode, the root layout renders it.
export const mediaAdd = $state({
	open: false,
	status: 'active' as 'active' | 'planned',
	/** Optional starting query, e.g. the library search bar's + seeds it. */
	query: ''
});

export function openMediaAdd(status: 'active' | 'planned', query = ''): void {
	mediaAdd.status = status;
	mediaAdd.query = query;
	mediaAdd.open = true;
}

export function closeMediaAdd(): void {
	mediaAdd.open = false;
}
