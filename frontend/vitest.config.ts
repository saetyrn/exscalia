// Standalone vitest config: the unit tests cover plain .ts modules (merge,
// sync logic), so they run without the SvelteKit plugin or a DOM.
import { defineConfig } from 'vitest/config';

export default defineConfig({
	test: {
		include: ['src/**/*.test.ts'],
		environment: 'node'
	},
	resolve: {
		// Mirror SvelteKit's alias so modules under test can import '$lib/…'.
		alias: { $lib: new URL('./src/lib', import.meta.url).pathname }
	}
});
