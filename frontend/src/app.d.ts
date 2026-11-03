// See https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare global {
	// Commit the frontend was built from (vite.config.ts `define`).
	const __BUILD_SHA__: string;

	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		interface PageState {
			// Set on an entry the recipe screens slid into (lib/recipes/nav.ts),
			// so their back pops history instead of pushing the fallback.
			recipesBack?: boolean;
		}
		// interface Platform {}
	}
}

export {};
