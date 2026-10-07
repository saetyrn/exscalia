import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
// @ts-expect-error -- no @types/node in this project; the config runs under Node
import { execSync } from 'node:child_process';
import { defineConfig } from 'vite';

// Commit the bundle was built from, baked in as __BUILD_SHA__ (Settings shows
// it on desktop). desktop/build-frontend.sh computes the same value, passes it
// in BUILD_SHA and stamps it next to the bundle for publish-update.sh.
// "-dirty" = frontend/ or desktop/ had uncommitted changes; builds without a
// .git (the web Docker image) get "unknown". kit.version stays the per-build
// timestamp: the service worker cache name and the web update poll need two
// builds of one dirty commit to differ.
function buildSha(): string {
	// @ts-expect-error -- Node's process, as above
	const fromEnv: string | undefined = process.env.BUILD_SHA;
	if (fromEnv) return fromEnv;
	try {
		const git = (args: string): string =>
			execSync(`git ${args}`, { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
		const sha = git('rev-parse --short HEAD');
		return git('status --porcelain -- :/frontend :/desktop') ? `${sha}-dirty` : sha;
	} catch {
		return 'unknown';
	}
}

export default defineConfig({
	define: {
		__BUILD_SHA__: JSON.stringify(buildSha())
	},
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
