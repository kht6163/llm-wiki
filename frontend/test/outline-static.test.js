import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { loadStatic } from "./static-test-utils.js";

let intersections;
let mutations;

beforeEach(() => {
  intersections = [];
  mutations = [];
  vi.stubGlobal("IntersectionObserver", class {
    constructor(callback, options) {
      this.callback = callback;
      this.options = options;
      this.observe = vi.fn();
      this.disconnect = vi.fn();
      intersections.push(this);
    }
  });
  vi.stubGlobal("MutationObserver", class {
    constructor(callback) {
      this.callback = callback;
      this.observe = vi.fn();
      mutations.push(this);
    }
  });
});

afterEach(() => {
  history.replaceState(null, "", "/");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function page(headings = "") {
  document.body.innerHTML = `<main id="doc-rendered">${headings}</main><nav id="outline"></nav>`;
  document.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach((h) => {
    h.scrollIntoView = vi.fn();
    h.getBoundingClientRect = () => ({ top: 200 });
  });
}

describe("outline.js", () => {
  test("does not initialize when either required container is absent", async () => {
    await loadStatic("outline");
    document.body.innerHTML = '<main id="doc-rendered"></main>';
    await loadStatic("outline");
    expect(mutations).toHaveLength(0);
  });

  test("shows and rebuilds the empty outline", async () => {
    page();
    await loadStatic("outline");
    expect(document.querySelector("#outline").innerHTML).toBe('<p class="muted">목차 없음</p>');
    expect(mutations[0].observe).toHaveBeenCalledWith(document.querySelector("#doc-rendered"), { childList: true });
    mutations[0].callback();
    expect(document.querySelector("#outline").textContent).toBe("목차 없음");
  });

  test("builds unique slugs, scrolls with motion preference and replaces hash", async () => {
    page('<h1>Hello, World!</h1><h2>Hello World</h2><h3>!!!</h3><h4 id="kept">Kept</h4>');
    const motion = { matches: false };
    vi.stubGlobal("matchMedia", vi.fn(() => motion));
    await loadStatic("outline");

    const heads = [...document.querySelectorAll("h1,h2,h3,h4")];
    expect(heads.map((h) => h.id)).toEqual(["hello-world", "hello-world-2", "section", "kept"]);
    expect([...document.querySelectorAll("#outline li")].map((li) => li.className)).toEqual(["ol-h1", "ol-h2", "ol-h3", "ol-h4"]);
    expect(intersections[0].options).toEqual({ rootMargin: "0px 0px -70% 0px", threshold: 0 });
    expect(intersections[0].observe).toHaveBeenCalledTimes(4);

    document.querySelector('a[href="#hello-world"]').click();
    expect(heads[0].scrollIntoView).toHaveBeenLastCalledWith({ behavior: "smooth", block: "start" });
    expect(location.hash).toBe("#hello-world");
    motion.matches = true;
    document.querySelector('a[href="#hello-world-2"]').click();
    expect(heads[1].scrollIntoView).toHaveBeenLastCalledWith({ behavior: "auto", block: "start" });
  });

  test("highlights visible, passed and fallback headings with ARIA state", async () => {
    page('<h1>One</h1><h2>Two</h2>');
    vi.stubGlobal("matchMedia", undefined);
    await loadStatic("outline");
    const [one, two] = document.querySelectorAll("h1,h2");
    const observer = intersections[0];

    observer.callback([{ target: two, isIntersecting: true }]);
    expect(document.querySelector('a[href="#two"]').getAttribute("aria-current")).toBe("location");
    expect(document.querySelector('a[href="#one"]').hasAttribute("aria-current")).toBe(false);

    two.getBoundingClientRect = () => ({ top: 100 });
    observer.callback([{ target: two, isIntersecting: false }]);
    expect(document.querySelector('a[href="#two"]').classList.contains("active")).toBe(true);

    two.getBoundingClientRect = () => ({ top: 200 });
    one.getBoundingClientRect = () => ({ top: 200 });
    observer.callback([]);
    expect(document.querySelector('a[href="#one"]').classList.contains("active")).toBe(true);
  });

  test("reveals valid and malformed initial hashes once and ignores missing targets", async () => {
    page('<h1 id="한글">Korean</h1>');
    document.querySelector("h1").scrollIntoView = vi.fn();
    history.replaceState(null, "", "#%ED%95%9C%EA%B8%80");
    await loadStatic("outline");
    expect(document.querySelector("h1").scrollIntoView).toHaveBeenCalledWith({ behavior: "auto", block: "start" });
    mutations[0].callback();
    expect(document.querySelector("h1").scrollIntoView).toHaveBeenCalledTimes(1);

    page('<h1 id="%E0%A4%A">Broken hash</h1>');
    history.replaceState(null, "", "#%E0%A4%A");
    await loadStatic("outline");
    expect(document.querySelector("h1").scrollIntoView).toHaveBeenCalled();

    page("<h1>Other</h1>");
    history.replaceState(null, "", "#missing");
    await loadStatic("outline");
    expect(document.querySelector("h1").scrollIntoView).not.toHaveBeenCalled();
  });

  test("disconnects old observers on live rebuild and handles missing observer support", async () => {
    page("<h1>One</h1>");
    await loadStatic("outline");
    const first = intersections[0];
    mutations[0].callback();
    expect(first.disconnect).toHaveBeenCalled();

    document.querySelector("#doc-rendered").replaceChildren();
    mutations[0].callback();
    expect(intersections[1].disconnect).toHaveBeenCalled();

    vi.unstubAllGlobals();
    delete window.IntersectionObserver;
    vi.stubGlobal("MutationObserver", class {
      constructor(callback) { this.callback = callback; }
      observe = vi.fn();
    });
    page("<h1>No observer</h1>");
    await loadStatic("outline");
    expect(document.querySelector("#outline a").textContent).toBe("No observer");
  });
});
