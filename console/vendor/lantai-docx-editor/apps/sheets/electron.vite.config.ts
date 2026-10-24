import react from '@vitejs/plugin-react'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import { resolve } from 'node:path'

export default defineConfig({
  main: {
    // @genoffice/* workspace packages ship TS source (no build step, no
    // compiled entry point) — externalizing them makes Node's ESM loader try
    // to resolve their relative imports at runtime and fail. Bundle those;
    // Keep every npm dependency inside the runtime bundle. Lantai copies only
    // out/** into its packaged Resources, so external packages would be
    // unresolvable there even though the standalone Sheets app has node_modules.
    plugins: [
      externalizeDepsPlugin({
        exclude: [
          '@genoffice/ai-provider',
          '@genoffice/agent-core',
          '@genoffice/docx-engine',
          '@genoffice/file-parse',
          '@genoffice/electron-utils',
          '@genoffice/i18n',
          '@lantai/office-ai-bridge',
          'fast-xml-parser',
          'jszip',
          'zod',
        ],
      }),
    ],
    build: {
      rollupOptions: {
        input: { lantai: resolve(__dirname, 'src/main/lantai-entry.ts') },
      },
    },
  },
  preload: {
    // Sandboxed preload scripts cannot require arbitrary npm packages at runtime.
    plugins: [],
  },
  renderer: {
    plugins: [react()],
  },
})
