import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			adapter: adapter({
				fallback: 'index.html'
			}),

			// Registered by hand in +layout.svelte — web only. The desktop app
			// has the real offline sync engine; a service worker there would
			// only get in its way.
			serviceWorker: {
				register: false
			}
		})
	],
	server: {
		proxy: {
			'/api': 'http://localhost:8600'
		}
	}
});
