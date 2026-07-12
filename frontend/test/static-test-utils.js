import { vi } from "vitest";

export async function loadStatic(name) {
  vi.resetModules();
  const modules = {
    datetime: () => import("../../src/llm_wiki/web/static/datetime.js"),
    outline: () => import("../../src/llm_wiki/web/static/outline.js"),
    preview: () => import("../../src/llm_wiki/web/static/preview.js"),
    props: () => import("../../src/llm_wiki/web/static/props.js"),
    related: () => import("../../src/llm_wiki/web/static/related.js"),
    tasks: () => import("../../src/llm_wiki/web/static/tasks.js"),
  };
  await modules[name]();
}

export async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

