// The `client` vitest project's environment (vitest.config.ts): Node, with
// modules transformed by Vite's client environment, so `.svelte.ts` files
// compile for the browser and their effects run.
import type { Environment } from 'vitest/environments';

export default {
	name: 'svelte-client',
	viteEnvironment: 'client',
	setup() {
		return { teardown() {} };
	}
} satisfies Environment;
