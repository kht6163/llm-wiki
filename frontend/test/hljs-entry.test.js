import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const highlighter = vi.hoisted(() => ({
  highlightElement: vi.fn((element) => element.setAttribute("data-highlighted", "yes")),
}));

vi.mock("highlight.js/lib/common", () => ({ default: highlighter }));

describe("reading-page highlighting lifecycle", () => {
  let observers;

  beforeEach(() => {
    vi.resetModules();
    highlighter.highlightElement.mockClear();
    observers = [];
    globalThis.MutationObserver = class {
      constructor(callback) { this.callback = callback; observers.push(this); }
      observe(target, options) { this.target = target; this.options = options; }
    };
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  test("runs immediately, highlights only fresh rendered code, and observes live replacements", async () => {
    Object.defineProperty(document, "readyState", { configurable: true, value: "complete" });
    document.body.innerHTML = [
      '<main id="doc-rendered" class="rendered">',
      '<pre><code id="fresh">const x = 1;</code></pre>',
      '<pre><code data-highlighted="yes">done</code></pre>',
      "</main>",
    ].join("");
    await import("../src/hljs-entry.js");
    expect(highlighter.highlightElement).toHaveBeenCalledOnce();
    expect(highlighter.highlightElement.mock.calls[0][0].id).toBe("fresh");
    expect(observers).toHaveLength(1);
    expect(observers[0].target.id).toBe("doc-rendered");
    expect(observers[0].options).toEqual({ childList: true, subtree: true });

    document.getElementById("doc-rendered").insertAdjacentHTML("beforeend", '<pre><code id="new">new</code></pre>');
    observers[0].callback();
    expect(highlighter.highlightElement.mock.calls.at(-1)[0].id).toBe("new");
    window.WikiHljs();
    expect(highlighter.highlightElement).toHaveBeenCalledTimes(2);
  });

  test("waits for DOMContentLoaded and tolerates individual highlighter failures", async () => {
    Object.defineProperty(document, "readyState", { configurable: true, value: "loading" });
    document.body.innerHTML = '<div class="rendered"><pre><code id="bad">bad</code></pre><pre><code id="good">good</code></pre></div>';
    highlighter.highlightElement
      .mockImplementationOnce(() => { throw new Error("unknown language"); })
      .mockImplementationOnce((element) => element.setAttribute("data-highlighted", "yes"));
    await import("../src/hljs-entry.js");
    expect(highlighter.highlightElement).not.toHaveBeenCalled();
    document.dispatchEvent(new Event("DOMContentLoaded"));
    expect(highlighter.highlightElement).toHaveBeenCalledTimes(2);
    expect(observers).toHaveLength(0);
  });

  test("skips language-mermaid code blocks so Mermaid can use the source", async () => {
    Object.defineProperty(document, "readyState", { configurable: true, value: "complete" });
    document.body.innerHTML = [
      '<main id="doc-rendered" class="rendered">',
      '<pre><code class="language-python" id="py">x = 1</code></pre>',
      '<pre><code class="language-mermaid" id="mm">graph TD\n  A-->B</code></pre>',
      "</main>",
    ].join("");
    await import("../src/hljs-entry.js");
    expect(highlighter.highlightElement).toHaveBeenCalledOnce();
    expect(highlighter.highlightElement.mock.calls[0][0].id).toBe("py");
  });
});
