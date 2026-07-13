import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    setupFiles: ["./test/setup.js"],
    coverage: {
      provider: "v8",
      allowExternal: true,
      include: [
        "src/**/*.{js,jsx}",
        "../src/llm_wiki/web/static/{datetime,editor,graph,merge,palette,preview,props,realtime,related,search,share,tasks,outline,shell}.js",
      ],
      exclude: ["node_modules/**", "**/coverage/**", "../src/llm_wiki/web/static/vendor/**"],
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
