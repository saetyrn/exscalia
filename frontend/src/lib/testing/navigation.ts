// Stand-in for SvelteKit's `$app/navigation` under vitest, which runs without
// the kit plugin. The tab strip imports `goto`, and the stores reach the tab
// strip to carry tabs along with renamed notes, so every test that loads the
// stores would otherwise fail to import. Tests that assert navigation still
// `vi.mock('$app/navigation', …)` over this.
export async function goto(): Promise<void> {}
