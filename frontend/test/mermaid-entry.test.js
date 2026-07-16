import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mermaidApi = vi.hoisted(() => ({
  initialize: vi.fn(),
  run: vi.fn(async ({ nodes }) => {
    for (const n of nodes) n.setAttribute("data-processed", "true");
  }),
}));

vi.mock("mermaid", () => ({ default: mermaidApi }));

describe("reading-page Mermaid lifecycle", () => {
  let observers;

  beforeEach(() => {
    vi.resetModules();
    mermaidApi.initialize.mockClear();
    mermaidApi.run.mockClear();
    mermaidApi.run.mockImplementation(async ({ nodes }) => {
      for (const n of nodes) n.setAttribute("data-processed", "true");
    });
    observers = [];
    globalThis.MutationObserver = class {
      constructor(callback) { this.callback = callback; observers.push(this); }
      observe(target, options) { this.target = target; this.options = options; }
    };
    document.documentElement.removeAttribute("data-theme");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete window.WikiMermaid;
  });

  test("initializes, converts language-mermaid fences, runs mermaid, observes live swaps", async () => {
    Object.defineProperty(document, "readyState", { configurable: true, value: "complete" });
    document.body.innerHTML = [
      '<main id="doc-rendered" class="rendered">',
      '<pre><code class="language-mermaid" id="src">graph TD\n  A-->B</code></pre>',
      '<pre><code class="language-python">x = 1</code></pre>',
      "</main>",
    ].join("");
    await import("../src/mermaid-entry.js");

    expect(mermaidApi.initialize).toHaveBeenCalledWith(
      expect.objectContaining({ startOnLoad: false, theme: "default" }),
    );
    expect(mermaidApi.run).toHaveBeenCalledOnce();
    const nodes = mermaidApi.run.mock.calls[0][0].nodes;
    expect(nodes).toHaveLength(1);
    expect(nodes[0].className).toBe("mermaid");
    expect(nodes[0].textContent).toContain("graph TD");
    expect(document.querySelector("pre code.language-mermaid")).toBeNull();
    expect(document.querySelector("div.mermaid[data-processed]")).toBeTruthy();
    expect(window.WikiMermaid).toBeTruthy();

    expect(observers).toHaveLength(1);
    expect(observers[0].target.id).toBe("doc-rendered");

    // Live re-render: new fence appears under #doc-rendered.
    document.getElementById("doc-rendered").insertAdjacentHTML(
      "beforeend",
      '<pre><code class="language-mermaid">flowchart LR\n  X-->Y</code></pre>',
    );
    await observers[0].callback();
    expect(mermaidApi.run).toHaveBeenCalledTimes(2);
    const second = mermaidApi.run.mock.calls[1][0].nodes;
    expect(second[0].textContent).toContain("flowchart LR");
  });

  test("uses dark theme when data-theme=dark and tolerates mermaid.run failures", async () => {
    document.documentElement.setAttribute("data-theme", "dark");
    Object.defineProperty(document, "readyState", { configurable: true, value: "loading" });
    document.body.innerHTML = '<div class="rendered"><pre><code class="language-mermaid">graph TD\n  A-->B</code></pre></div>';
    mermaidApi.run.mockRejectedValueOnce(new Error("bad diagram"));
    await import("../src/mermaid-entry.js");
    expect(mermaidApi.initialize).toHaveBeenCalledWith(
      expect.objectContaining({ theme: "dark", startOnLoad: false }),
    );
    expect(mermaidApi.run).not.toHaveBeenCalled();
    document.dispatchEvent(new Event("DOMContentLoaded"));
    // Allow the async boot() path to settle after the rejected run.
    await vi.waitFor(() => expect(mermaidApi.run).toHaveBeenCalledOnce());
    // No throw — invalid diagrams leave the converted node in place.
    expect(document.querySelector("div.mermaid")).toBeTruthy();
  });

  test("skips already-queued nodes and supports bare div.mermaid", async () => {
    Object.defineProperty(document, "readyState", { configurable: true, value: "complete" });
    document.body.innerHTML = [
      '<main id="doc-rendered" class="rendered">',
      '<div class="mermaid">sequenceDiagram\n  A->>B: hi</div>',
      '<div class="mermaid" data-processed="true">already done</div>',
      '<div class="mermaid" data-mermaid-pending="1">already queued</div>',
      "</main>",
    ].join("");
    await import("../src/mermaid-entry.js");
    expect(mermaidApi.run).toHaveBeenCalledOnce();
    const nodes = mermaidApi.run.mock.calls[0][0].nodes;
    expect(nodes).toHaveLength(1);
    expect(nodes[0].textContent).toContain("sequenceDiagram");

    // Manual re-run should not re-process pending/processed nodes.
    await window.WikiMermaid.run(document.getElementById("doc-rendered"));
    expect(mermaidApi.run).toHaveBeenCalledOnce();
  });

  test("skips nested non-direct fences and already-done fences; empty roots are no-ops", async () => {
    Object.defineProperty(document, "readyState", { configurable: true, value: "complete" });
    document.body.innerHTML = [
      '<main id="doc-rendered" class="rendered">',
      // Not a direct child of PRE (selector is pre > code) — ignored.
      '<pre><span><code class="language-mermaid">graph TD\n  A-->B</code></span></pre>',
      '<pre><code class="language-mermaid" data-mermaid-done="1">graph TD\n  X-->Y</code></pre>',
      "</main>",
    ].join("");
    await import("../src/mermaid-entry.js");
    expect(mermaidApi.run).not.toHaveBeenCalled();
    await window.WikiMermaid.run(document.getElementById("doc-rendered"));
    expect(mermaidApi.run).not.toHaveBeenCalled();
    await window.WikiMermaid.run(null);
    expect(mermaidApi.run).not.toHaveBeenCalled();
  });

  test("converts empty-body PRE fences", async () => {
    Object.defineProperty(document, "readyState", { configurable: true, value: "complete" });
    document.body.innerHTML = '<main id="doc-rendered" class="rendered"></main>';
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.className = "language-mermaid";
    Object.defineProperty(code, "textContent", { configurable: true, get: () => null });
    pre.appendChild(code);
    document.getElementById("doc-rendered").appendChild(pre);

    await import("../src/mermaid-entry.js");
    expect(mermaidApi.run).toHaveBeenCalledOnce();
    const nodes = mermaidApi.run.mock.calls[0][0].nodes;
    expect(nodes).toHaveLength(1);
    expect(nodes[0].className).toBe("mermaid");
    expect(nodes[0].textContent).toBe("");
  });
});
