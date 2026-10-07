// Standalone vitest config: the unit tests cover plain .ts modules (merge,
// sync logic, store actions), so they run without SvelteKit or a DOM. The
// svelte plugin is here for one reason — `.svelte.ts` rune modules are not
// valid JS until it compiles them — with configFile: false because the
// project has no svelte.config.js (see vite.config.ts) and the lookup would
// only warn about it.
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { configDefaults, defineConfig } from 'vitest/config';

export default defineConfig({
	plugins: [svelte({ configFile: false, compilerOptions: { runes: true } })],
	test: {
		// Vitest blanks every CSS import by default; a `?raw` import is the
		// stylesheet's text (mobilefloor.test.ts reads app.css), so let it through.
		css: { include: [/[?&]raw\b/] },
		projects: [
			{
				extends: true,
				test: {
					name: 'node',
					include: ['src/**/*.test.ts'],
					exclude: [...configDefaults.exclude, 'src/**/*.svelte.test.ts'],
					environment: 'node'
				}
			},
			// Rune tests. The node project builds `.svelte.ts` modules for the
			// server, where `$effect` never runs and `untrack` is a plain call,
			// so what an effect subscribes to cannot be tested there.
			// `*.svelte.test.ts` files get the browser build of their modules
			// and of svelte itself.
			{
				extends: true,
				resolve: { conditions: ['browser'] },
				test: {
					name: 'client',
					include: ['src/**/*.svelte.test.ts'],
					environment: './src/lib/testing/clientenv.ts'
				}
			}
		]
	},
	resolve: {
		// Mirror SvelteKit's alias so modules under test can import '$lib/…'.
		alias: {
			$lib: new URL('./src/lib', import.meta.url).pathname,
			'$app/navigation': new URL('./src/lib/testing/navigation.ts', import.meta.url).pathname
		}
	}
});
