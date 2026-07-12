import { afterEach, beforeEach, vi } from "vitest";

const observerInstances = {
  IntersectionObserver: [],
  MutationObserver: [],
};

function observerClass(name) {
  return class {
    constructor(callback, options) {
      this.callback = callback;
      this.options = options;
      this.observe = vi.fn();
      this.disconnect = vi.fn();
      observerInstances[name].push(this);
    }
  };
}

export function getObservers(name) {
  return observerInstances[name];
}

export function useStaticIsolation() {
  let listeners;
  let restoreListenerSpies;

  beforeEach(() => {
    listeners = [];
    restoreListenerSpies = [];
    for (const target of [document, window]) {
      const add = target.addEventListener.bind(target);
      const remove = target.removeEventListener.bind(target);
      const spy = vi.spyOn(target, "addEventListener").mockImplementation((type, listener, options) => {
        listeners.push({ remove, type, listener, options });
        add(type, listener, options);
      });
      restoreListenerSpies.push(spy);
    }
    for (const name of Object.keys(observerInstances)) {
      observerInstances[name] = [];
      vi.stubGlobal(name, observerClass(name));
    }
  });

  afterEach(() => {
    for (const { remove, type, listener, options } of listeners) remove(type, listener, options);
    for (const instances of Object.values(observerInstances)) {
      for (const observer of instances) observer.disconnect();
    }
    vi.clearAllTimers();
    vi.useRealTimers();
    for (const spy of restoreListenerSpies) spy.mockRestore();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    document.body.replaceChildren();
    document.documentElement.removeAttribute("data-theme");
    delete window.WIKI;
    delete window.WikiLocalizeTime;
    delete window.WikiMdEditor;
    delete window.WikiPalette;
    delete window.WikiShell;
  });
}

export async function loadStatic(name) {
  vi.resetModules();
  const modules = {
    datetime: () => import("../../src/llm_wiki/web/static/datetime.js"),
    editor: () => import("../../src/llm_wiki/web/static/editor.js"),
    outline: () => import("../../src/llm_wiki/web/static/outline.js"),
    palette: () => import("../../src/llm_wiki/web/static/palette.js"),
    preview: () => import("../../src/llm_wiki/web/static/preview.js"),
    props: () => import("../../src/llm_wiki/web/static/props.js"),
    realtime: () => import("../../src/llm_wiki/web/static/realtime.js"),
    related: () => import("../../src/llm_wiki/web/static/related.js"),
    shell: () => import("../../src/llm_wiki/web/static/shell.js"),
    tasks: () => import("../../src/llm_wiki/web/static/tasks.js"),
  };
  await modules[name]();
}

export async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}
