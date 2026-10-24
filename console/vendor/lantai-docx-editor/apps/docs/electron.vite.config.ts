import react from '@vitejs/plugin-react'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import { resolve } from 'node:path'

export default defineConfig({
  // Main and preload use only electron + node builtins; bundle everything so
  // the packaged app doesn't rely on node_modules at runtime.
  // @genoffice/* deps ship as raw TS source with extensionless imports, so they
  // must be bundled — externalizing them yields ERR_MODULE_NOT_FOUND under Node
  // (same setup as apps/slides).
  main: {
    plugins: [
      externalizeDepsPlugin({
        exclude: ['@genoffice/electron-utils', '@lantai/office-ai-bridge'],
      }),
    ],
    build: {
      rollupOptions: {
        input: {
          lantai: resolve(__dirname, 'src/main/lantai-entry.ts'),
        },
      },
    },
  },
  preload: {},
  renderer: {
    plugins: [react()],
    build: {
      minify: 'esbuild',
    },
    server: {
      // Overridable so multiple genoffice dev instances can coexist (default 5173).
      port: Number(process.env.DOCS_DEV_PORT) || 5173,
      strictPort: Boolean(process.env.DOCS_DEV_PORT),
    },
  },
})
