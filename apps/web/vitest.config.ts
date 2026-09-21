import { configDefaults, defineConfig } from "vitest/config";

export default defineConfig({
  // Next compiles JSX with the automatic runtime; the test transform has to
  // match, otherwise components without an explicit React import cannot render.
  esbuild: { jsx: "automatic" },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    // Playwright owns everything under ``e2e/``; vitest must not pick it up.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
