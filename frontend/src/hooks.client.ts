import type { HandleClientError } from '@sveltejs/kit';

/** SvelteKit's default swaps an unexpected error's message for 'Internal
 * Error'. The desktop release build and the iPad shell have no console to read,
 * so the error page is the only place a failure shows: pass the real message
 * through, and still log the error for wherever a console does exist. */
export const handleError: HandleClientError = ({ error, message }) => {
	console.error(error);
	return { message: error instanceof Error ? error.message : message };
};
