import { beforeEach, describe, expect, test, vi } from "vitest";
import { flush, getObservers, loadStatic, useStaticIsolation } from "./static-test-utils.js";

useStaticIsolation();

class FakeNode {
  constructor(data) {
    this.values = data;
    this.style = vi.fn();
    this.select = vi.fn();
  }

  data(key) { return this.values[key]; }
}

class FakeNodes extends Array {
  empty() { return this.length === 0; }
  unselect() { this.forEach((node) => { node.selected = false; }); }
}

let instances;

function installCytoscape({ zoom = 2 } = {}) {
  instances = [];
  const factory = vi.fn((options) => {
    const nodes = new FakeNodes(...options.elements
      .filter((element) => !Object.hasOwn(element.data, "source"))
      .map((element) => new FakeNode(element.data)));
    const instance = {
      options,
      nodes: vi.fn(() => nodes),
      elements: vi.fn(() => options.elements),
      batch: vi.fn((callback) => callback()),
      resize: vi.fn(),
      fit: vi.fn(),
      zoom: vi.fn(() => zoom),
      center: vi.fn(),
      style: vi.fn(),
      destroy: vi.fn(),
      on: vi.fn((type, selector, callback) => { instance.tap = callback; }),
      layout: vi.fn(() => ({
        one: vi.fn((type, callback) => { instance.layoutStop = callback; }),
        run: vi.fn(() => instance.layoutStop()),
      })),
    };
    instances.push(instance);
    return instance;
  });
  vi.stubGlobal("cytoscape", factory);
  return factory;
}

function graphPage() {
  document.body.innerHTML = `
    <input id="root" value="root note.md">
    <input id="depth" value="2">
    <input id="folder" value="team docs">
    <input id="tag" value="important">
    <input id="include-unresolved" type="checkbox">
    <button id="graph-apply"></button>
    <span id="ginfo"></span>
    <div id="cy" tabindex="0"></div>
    <span id="gkb"></span>
    <p id="gempty" hidden></p>`;
}

function response(nodes, { edges = [], truncated = false } = {}) {
  return { nodes, edges, truncated };
}

function node(id, { exists = true, root = false } = {}) {
  return { id, label: id.toUpperCase(), exists, is_root: root };
}

function key(target, value) {
  const event = new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  graphPage();
  vi.stubGlobal("location", { href: "" });
});

describe("graph.js", () => {
  test("does nothing outside the graph page", async () => {
    document.body.replaceChildren();
    await loadStatic("graph");
    expect(window.WikiGraph).toBeUndefined();
  });

  test("renders, filters, resizes, recolours and navigates without trapping Tab", async () => {
    const mediaHandlers = [];
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      addEventListener: vi.fn((type, callback) => mediaHandlers.push(callback)),
    })));
    installCytoscape();
    const pending = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => pending.push(resolve))));

    await loadStatic("graph");
    const info = document.querySelector("#ginfo");
    const canvas = document.querySelector("#cy");
    expect(info.textContent).toBe("불러오는 중…");
    expect(info.getAttribute("aria-busy")).toBe("true");

    // These calls cover the safe pre-data state while the initial request is pending.
    window.WikiGraph.fit();
    window.WikiGraph.keyboardFocus(1);
    expect(window.WikiGraph.openNode(null)).toBe(false);
    getObservers("MutationObserver")[0].callback([]);

    pending.shift()({ json: () => Promise.resolve(response(
      [node("b", { exists: false }), node("a", { root: true }), node("c")],
      { edges: [{ id: "a-b", source: "a", target: "b", resolved: false }], truncated: true },
    )) });
    await flush();

    const first = instances[0];
    expect(fetch).toHaveBeenCalledWith(
      "/api/graph?depth=2&limit=500&include_unresolved=false&root=root%20note.md&folder=team%20docs&tag=important",
      { credentials: "same-origin" },
    );
    expect(info.textContent).toBe("3 노드 / 1 엣지 (일부 생략됨)");
    expect(info.hasAttribute("aria-busy")).toBe(false);
    expect(document.querySelector("#gempty").hidden).toBe(true);
    expect(first.batch).toHaveBeenCalled();
    expect(first.resize).toHaveBeenCalled();
    expect(first.fit).toHaveBeenCalled();
    expect(first.nodes()[1].style).toHaveBeenCalledWith(expect.objectContaining({ width: 96 }));

    // Both unresolved and existing tap targets take their respective open/no-open paths.
    first.tap({ target: first.nodes()[0] });
    expect(location.href).toBe("");
    first.tap({ target: first.nodes()[1] });
    expect(location.href).toBe("/doc/a");

    getObservers("MutationObserver")[0].callback([]);
    mediaHandlers[0]();
    expect(first.style).toHaveBeenCalledTimes(2);
    for (const [name, value] of [["--accent", "red"], ["--fg", "black"],
      ["--fg-faint", "gray"], ["--line", "silver"], ["--line-strong", "navy"],
      ["--inset", "white"]]) document.documentElement.style.setProperty(name, value);
    expect(window.WikiGraph.style()[0].style["background-color"]).toBe("red");

    expect(key(canvas, "Tab").defaultPrevented).toBe(false);
    expect(key(canvas, "Enter").defaultPrevented).toBe(false);
    for (const direction of ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"]) {
      expect(key(canvas, direction).defaultPrevented).toBe(true);
    }
    key(canvas, "Home");
    expect(document.querySelector("#gkb").textContent).toContain("A — 1/3");
    expect(key(canvas, "Enter").defaultPrevented).toBe(true);
    key(canvas, "ArrowRight");
    expect(document.querySelector("#gkb").textContent).toContain("없는 문서");
    expect(key(canvas, "Enter").defaultPrevented).toBe(false);
    document.querySelector("#gkb").remove();
    window.WikiGraph.keyboardFocus(1);
    expect(key(canvas, "x").defaultPrevented).toBe(false);

    // Empty filters and an enabled unresolved toggle build the shorter query.
    for (const id of ["root", "folder", "tag"]) document.getElementById(id).value = "";
    document.getElementById("include-unresolved").checked = true;
    document.getElementById("graph-apply").click();
    pending.shift()({ json: () => Promise.resolve(response([])) });
    await flush();
    expect(fetch.mock.calls[1][0]).toBe("/api/graph?depth=2&limit=500&include_unresolved=true");
    expect(first.destroy).toHaveBeenCalledOnce();
    expect(document.querySelector("#gempty").hidden).toBe(false);
    window.WikiGraph.fit();
    window.WikiGraph.keyboardFocus(-1);

    // Enter in a field has parity with the button; a non-Enter key is untouched.
    expect(key(document.querySelector("#root"), "x").defaultPrevented).toBe(false);
    const enter = key(document.querySelector("#tag"), "Enter");
    expect(enter.defaultPrevented).toBe(true);
    pending.shift()({ json: () => Promise.resolve(response([node("only", { root: true })])) });
    await flush();
    expect(instances[2].nodes()[0].style).toHaveBeenCalledWith(expect.objectContaining({ width: 96 }));

    // Exercise the uncapped and lower-capped size bands and zoom fallback.
    const middleReload = window.WikiGraph.reload();
    pending.shift()({ json: () => Promise.resolve(response(
      Array.from({ length: 9 }, (_, index) => node("m" + index)),
    )) });
    await middleReload;
    expect(instances[3].nodes()[0].style).toHaveBeenCalledWith(expect.objectContaining({ width: 50 }));

    installCytoscape({ zoom: 0 });
    const denseReload = window.WikiGraph.reload();
    pending.shift()({ json: () => Promise.resolve(response(
      Array.from({ length: 100 }, (_, index) => node("d" + index)),
    )) });
    await denseReload;
    const dense = instances[0];
    expect(dense.nodes()[0].style).toHaveBeenCalledWith(expect.objectContaining({ width: 18 }));
    window.WikiGraph.fit();
    expect(dense.zoom).toHaveBeenCalled();
  });

  test("shows the fetch failure and supports legacy theme listeners", async () => {
    const legacy = vi.fn();
    vi.stubGlobal("matchMedia", vi.fn(() => ({ addListener: legacy })));
    installCytoscape();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    await loadStatic("graph");
    await flush();
    expect(document.querySelector("#ginfo").textContent).toContain("불러오지 못했습니다");
    expect(document.querySelector("#ginfo").hasAttribute("aria-busy")).toBe(false);
    expect(legacy).toHaveBeenCalledOnce();
    legacy.mock.calls[0][0]();
  });

  test("works when system theme observation is unavailable", async () => {
    vi.stubGlobal("matchMedia", undefined);
    installCytoscape();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      json: () => Promise.resolve(response([node("plain")])),
    }));
    await loadStatic("graph");
    await flush();
    expect(document.querySelector("#ginfo").textContent).toContain("1 노드");

    vi.stubGlobal("matchMedia", vi.fn(() => ({})));
    await loadStatic("graph");
    await flush();
    expect(document.querySelector("#ginfo").textContent).toContain("1 노드");
  });
});
