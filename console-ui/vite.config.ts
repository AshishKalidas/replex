import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// The build lands inside the Python package so `pip install replex[console]` ships it.
// Fixed file names (no hashes) keep the committed build diff-able and let the Python
// report generator inline app.js / app.css into one self-contained HTML file.
const outDir =
  process.env.REPLEX_CONSOLE_OUT ??
  fileURLToPath(new URL("../src/replex/console/static", import.meta.url));

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir,
    emptyOutDir: true,
    assetsInlineLimit: 0,
    cssCodeSplit: false,
    modulePreload: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/app.js",
        assetFileNames: "assets/app.[ext]",
        inlineDynamicImports: true,
      },
    },
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
  test: {
    include: ["test/**/*.test.ts", "test/**/*.test.tsx"],
    // exFAT / network volumes create AppleDouble "._*" files next to sources.
    exclude: ["**/node_modules/**", "**/._*"],
    environment: "node",
  },
});
