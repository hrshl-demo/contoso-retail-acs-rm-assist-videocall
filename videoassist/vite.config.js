import { defineConfig } from 'vite';

// PUBLIC PATH PREFIX.
//
// On the VM, Caddy serves this app under https://<host>/video/* and `handle_path` STRIPS
// "/video" before proxying, so Express still sees "/". The browser, however, still asks for
// "/video/assets/...", so the BUNDLED asset URLs Vite writes into dist/index.html must carry
// the prefix. That is what `base` controls.
//
// `base` ONLY rewrites URLs Vite itself generates (script/link tags, imported assets).
// It does NOT touch fetch() string literals in application code. Those are handled at
// runtime via window.__VA_BASE__, injected by server.js — see client/main.js:api().
//
// The default is '/' ON PURPOSE, not '/video/'. The phase9 Container App still serves this
// SPA at the ROOT until Phase E retires it; baking '/video/' in as the default would make
// every asset 404 there. The VM deploy sets VA_BASE_PATH=/video/ explicitly
// (tools/deploy-videoassist-on-vm.sh). Once phase9 is gone the default can simply flip.
const base = process.env.VA_BASE_PATH || '/';

export default defineConfig({
  base,
  root: 'client',
  build: { outDir: '../dist', emptyOutDir: true },
});
