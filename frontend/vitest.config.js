import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    setupFiles: ["./test/setup.js"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{js,jsx}"],
      exclude: ["node_modules/**", "coverage/**", "../src/llm_wiki/web/static/vendor/**"],
      reporter: ["text", "json-summary"],
      thresholds: {
        lines: 100,
        statements: 100,
        functions: 100,
        branches: 100,
        perFile: true,
      },
    },
  },
});
