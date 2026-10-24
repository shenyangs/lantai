// @ts-nocheck
/// <reference types="vitest" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { nodeTestFiles } from "./scripts/test-environments.mjs";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  // Empty = same-origin; frontend and backend served together, no hardcoded host.
  // Use a dedicated Vite-prefixed key so unrelated shell BASE_URL values don't leak into the build.
  const apiBaseUrl = env.VITE_API_BASE_URL ?? "";
  const devProxyTarget =
    env.VITE_DEV_PROXY_TARGET?.trim() || "http://127.0.0.1:8088";

  return {
    define: {
      VITE_API_BASE_URL: JSON.stringify(apiBaseUrl),
      TOKEN: JSON.stringify(env.TOKEN || ""),
      MOBILE: false,
    },
    plugins: [react()],
    css: {
      modules: {
        localsConvention: "camelCase",
        generateScopedName: "[name]__[local]__[hash:base64:5]",
      },
      preprocessorOptions: {
        less: {
          javascriptEnabled: true,
        },
      },
    },
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
      // The embedded Office workspaces install their own React for their
      // standalone builds. The Console must always render with its root copy.
      dedupe: ["react", "react-dom"],
    },
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy: {
        "/api": {
          target: devProxyTarget,
          changeOrigin: true,
        },
      },
    },
    test: {
      globals: true,
      // Full-suite parallelism makes the heaviest interaction tests exceed
      // Vitest's 5-second default even though their assertions complete.
      testTimeout: 10_000,
      environmentOptions: {
        jsdom: {
          url: "http://localhost/",
        },
      },
      projects: [
        {
          extends: true,
          test: {
            name: "node",
            environment: "node",
            include: nodeTestFiles,
          },
        },
        {
          extends: true,
          test: {
            name: "dom",
            environment: "jsdom",
            setupFiles: ["./src/test/setup.ts"],
            exclude: [
              ...nodeTestFiles,
              "**/node_modules/**",
              "**/dist/**",
              "**/electron/**/*.test.cjs",
              "**/scripts/**/*.test.{mjs,cjs}",
              "**/vendor/lantai-docx-editor/**",
              "**/testConnectionMessage.test.ts",
            ],
          },
        },
      ],
      css: true,
      exclude: [
        "**/node_modules/**",
        "**/dist/**",
        "**/electron/**/*.test.cjs",
        // This contract uses Node's native test runner because the source-pin
        // parser is also consumed directly by repository scripts.  The
        // package test:run command executes it before Vitest.
        "**/scripts/**/*.test.{mjs,cjs}",
        // The vendored Office runtime owns separate per-workspace Vitest and
        // native-engine suites. Running them from the console cwd breaks their
        // fixture and sidecar paths, so the console suite must not rediscover
        // them as application tests.
        "**/vendor/lantai-docx-editor/**",
        // 旧测试用 node:test，与 vitest 不兼容，待迁移
        "**/testConnectionMessage.test.ts",
      ],
      coverage: {
        provider: "v8",
        reporter: ["text", "html", "json", "lcov"],
        include: ["src/**/*.{ts,tsx}"],
        exclude: [
          "src/test/**",
          "src/**/*.d.ts",
          "src/main.tsx",
          "src/vite-env.d.ts",
        ],
        thresholds: {
          statements: 25,
          branches: 24,
          functions: 20,
          lines: 25,
        },
      },
    },
    base: "./",
    build: {
      // Output to 观澜台's console directory,
      // so we don't need to copy files manually after build.
      // outDir: path.resolve(__dirname, "../src/lantai/console"),
      // emptyOutDir: true,
      cssCodeSplit: true,
      sourcemap: mode !== "production",
      // The UI vendor bundle intentionally contains Ant Design and related
      // component packages; keep the warning threshold aligned with that known
      // architecture so build output only reports unexpected regressions.
      chunkSizeWarningLimit: 9000,
      rollupOptions: {
        onwarn(warning, warn) {
          if (warning.code === "INEFFECTIVE_DYNAMIC_IMPORT") {
            return;
          }
          warn(warning);
        },
        output: {
          manualChunks(id) {
            // React core
            if (
              id.includes("node_modules/react/") ||
              id.includes("node_modules/react-dom/") ||
              id.includes("node_modules/react-router-dom/") ||
              id.includes("node_modules/scheduler/")
            ) {
              return "react-vendor";
            }
            // Ant Design and related component packages.
            if (
              id.includes("node_modules/antd/") ||
              id.includes("node_modules/antd-style/") ||
              id.includes("node_modules/@ant-design/")
            ) {
              return "ui-vendor";
            }
            // i18n
            if (
              id.includes("node_modules/i18next/") ||
              id.includes("node_modules/react-i18next/")
            ) {
              return "i18n-vendor";
            }
            // Markdown rendering
            if (
              id.includes("node_modules/react-markdown/") ||
              id.includes("node_modules/remark-gfm/") ||
              id.includes("node_modules/rehype") ||
              id.includes("node_modules/remark") ||
              id.includes("node_modules/unified/") ||
              id.includes("node_modules/mdast") ||
              id.includes("node_modules/hast") ||
              id.includes("node_modules/micromark")
            ) {
              return "markdown-vendor";
            }
            // Drag and drop
            if (id.includes("node_modules/@dnd-kit/")) {
              return "dnd-vendor";
            }
            // Utilities (dayjs, zustand, ahooks, etc.)
            if (
              id.includes("node_modules/dayjs/") ||
              id.includes("node_modules/zustand/") ||
              id.includes("node_modules/ahooks/") ||
              id.includes("node_modules/@vvo/tzdb/")
            ) {
              return "utils-vendor";
            }
          },
        },
      },
    },
  };
});
