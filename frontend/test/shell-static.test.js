import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { flush, loadStatic, useStaticIsolation } from "./static-test-utils.js";

useStaticIsolation();

let media;

function installMedia({ narrow = false, legacy = false } = {}) {
  const listeners = new Set();
  media = {
    matches: narrow,
    dispatch(value) {
      this.matches = value;
      listeners.forEach((listener) => listener({ matches: value, media: "(max-width: 860px)" }));
    },
  };
  if (legacy) media.addListener = (listener) => listeners.add(listener);
  else media.addEventListener = (type, listener) => type === "change" && listeners.add(listener);
  vi.stubGlobal("matchMedia", vi.fn(() => media));
  return media;
}

function shellPage({ right = true, tabs = true, tree = "", search = true, resizers = true } = {}) {
  document.body.innerHTML = `
    <div id="app-shell">
      <button id="left-a" data-action="toggle-left"></button>
      <button id="left-b" data-action="toggle-left"></button>
      <button id="right-a" data-action="toggle-right"></button>
      <button data-action="toggle-theme"></button>
      <aside id="sidebar-left">
        ${tabs ? `<div class="sb-tabs"><button class="sb-tab active" data-tab="files" aria-selected="true" tabindex="0"></button><button class="sb-tab" data-tab="search" aria-selected="false" tabindex="-1"></button></div><section data-panel="files"></section><section data-panel="search" hidden></section>` : ""}
        ${search ? '<input id="sb-search-input"><div id="sb-search-results" aria-live="polite"></div>' : ""}
        <div id="file-tree">${tree}</div>
        ${resizers ? '<div id="left-resizer" class="sb-resizer" data-resize="left" role="separator" tabindex="0" aria-valuemin="150" aria-valuemax="560"></div>' : ""}
      </aside>
      ${right ? `<aside id="sidebar-right"><div class="rp-tabs"><button class="rp-tab active" data-rp="outline" aria-selected="true" tabindex="0"></button><button class="rp-tab" data-rp="links" aria-selected="false" tabindex="-1"></button></div><section data-rp-panel="outline"></section><section data-rp-panel="links" hidden></section>${resizers ? '<div id="right-resizer" class="sb-resizer" data-resize="right" role="separator" tabindex="0" aria-valuemin="150" aria-valuemax="560"></div>' : ""}</aside>` : ""}
    </div>`;
}

function doc(path, title = path) {
  return `<a class="tree-row tree-doc" data-doc="${path}" href="/doc/${path}"><span class="tree-label">${title}</span></a>`;
}

function folder(path, children = "") {
  return `<details class="tree-folder" data-folder="${path}"><summary class="tree-row tree-folder-row" data-folder="${path}" tabindex="0"><span class="tree-label">${path}</span></summary><div class="tree-children">${children}</div></details>`;
}

async function boot(options = {}, wiki = { canWrite: true, csrf: "token" }) {
  installMedia(options);
  window.WIKI = wiki;
  await loadStatic("shell");
  return document.querySelector("#app-shell");
}

function pointer(type, init) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, init);
  return event;
}

async function finishRequest() {
  await flush();
  await flush();
}

beforeEach(() => {
  localStorage.clear();
  history.replaceState(null, "", "/");
  vi.stubGlobal("requestAnimationFrame", (callback) => callback());
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
});

afterEach(() => {
  delete HTMLElement.prototype.scrollIntoView;
  document.documentElement.style.removeProperty("--left-w");
  document.documentElement.style.removeProperty("--right-w");
});

describe("shell initialization and layout", () => {
  test("does nothing when the application shell is absent", async () => {
    await loadStatic("shell");
    expect(window.WikiShell).toBeUndefined();
    expect(document.body.childElementCount).toBe(0);
  });

  test("restores wide collapse state, toggle ARIA and auto-hides an empty right panel", async () => {
    localStorage.setItem("wiki-left-collapsed", "1");
    shellPage({ right: false });
    const shell = await boot();
    expect(shell.classList.contains("no-left")).toBe(true);
    expect(shell.classList.contains("no-right")).toBe(false);
    expect(document.querySelector("#left-a").getAttribute("aria-pressed")).toBe("false");
    expect(document.querySelector("#right-a").getAttribute("aria-pressed")).toBe("true");

    document.querySelector("[data-action=toggle-left]").click();
    expect(shell.classList.contains("no-left")).toBe(false);
    expect(localStorage.getItem("wiki-left-collapsed")).toBe("0");
    document.querySelector("[data-action=toggle-right]").click();
    expect(shell.classList.contains("no-right")).toBe(true);
    expect(localStorage.getItem("wiki-right-collapsed")).toBe("1");
  });

  test("survives unavailable storage and hides a contentless right panel only without a preference", async () => {
    shellPage();
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    const shell = await boot();
    document.querySelector("[data-action=toggle-left]").click();
    expect(shell.classList.contains("no-right")).toBe(false);
    expect(shell.classList.contains("no-left")).toBe(true);
    expect(getItem).toHaveBeenCalled();
    expect(setItem).toHaveBeenCalled();

    getItem.mockRestore();
    setItem.mockRestore();
    localStorage.setItem("wiki-right-collapsed", "0");
    shellPage();
    await boot();
    expect(document.querySelector("#app-shell").classList.contains("no-right")).toBe(false);
  });

  test("closes narrow overlays by exclusivity, backdrop, Escape and media changes", async () => {
    vi.useFakeTimers();
    shellPage();
    const shell = await boot({ narrow: true });
    expect(shell.classList.contains("no-left")).toBe(true);
    expect(shell.classList.contains("no-right")).toBe(true);

    document.querySelector("#left-a").click();
    vi.runOnlyPendingTimers();
    expect(shell.classList.contains("no-left")).toBe(false);
    expect(shell.classList.contains("no-right")).toBe(true);
    expect(document.querySelector(".sb-backdrop.show")).not.toBeNull();
    document.querySelector("#right-a").click();
    expect(shell.classList.contains("no-left")).toBe(true);
    expect(shell.classList.contains("no-right")).toBe(false);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(shell.classList.contains("no-right")).toBe(true);
    vi.runAllTimers();
    expect(document.querySelector(".sb-backdrop")).toBeNull();

    document.querySelector("#left-a").click();
    document.querySelector(".sb-backdrop").click();
    expect(shell.classList.contains("no-left")).toBe(true);
    vi.runAllTimers();
    media.dispatch(false);
    expect(shell.classList.contains("no-left")).toBe(false);
    media.dispatch(true);
    expect(shell.classList.contains("no-left")).toBe(true);
  });

  test("supports legacy media listeners and ignores unrelated Escape", async () => {
    shellPage();
    const shell = await boot({ legacy: true });
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    media.dispatch(true);
    expect(shell.classList.contains("no-left")).toBe(true);
    expect(shell.classList.contains("no-right")).toBe(true);
  });

  test("works without a WIKI object or media change subscription API", async () => {
    shellPage();
    const listeners = installMedia();
    delete listeners.addEventListener;
    delete window.WIKI;
    await loadStatic("shell");
    expect(window.WikiShell).toBeDefined();
    expect(document.querySelector("#app-shell").classList.contains("no-left")).toBe(false);
  });

  test("auto-hides a contentless right panel and can close already-open narrow panels", async () => {
    shellPage();
    document.querySelector("#sidebar-right").replaceChildren();
    const shell = await boot({ narrow: false });
    expect(shell.classList.contains("no-right")).toBe(true);
    window.WikiShell.toggleRight();
    expect(shell.classList.contains("no-right")).toBe(false);
    window.WikiShell.toggleRight();
    expect(shell.classList.contains("no-right")).toBe(true);

    media.matches = true;
    window.WikiShell.toggleLeft();
    expect(shell.classList.contains("no-left")).toBe(true);
    window.WikiShell.toggleRight();
    expect(shell.classList.contains("no-right")).toBe(false);
    window.WikiShell.toggleRight();
    expect(shell.classList.contains("no-right")).toBe(true);
  });

  test("cycles dark, light and system themes and persists each state", async () => {
    shellPage();
    await boot();
    const toggle = document.querySelector("[data-action=toggle-theme]");
    toggle.click();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(localStorage.getItem("wiki-theme")).toBe("dark");
    toggle.click();
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    toggle.click();
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(localStorage.getItem("wiki-theme")).toBe("");
  });

  test("guards destructive forms with confirm while allowing accepted and ordinary submits", async () => {
    shellPage();
    await boot();
    document.body.insertAdjacentHTML("beforeend", '<form id="guard" data-confirm="정말?"></form><form id="plain"></form>');
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const denied = new Event("submit", { bubbles: true, cancelable: true });
    document.querySelector("#guard").dispatchEvent(denied);
    expect(denied.defaultPrevented).toBe(true);
    const accepted = new Event("submit", { bubbles: true, cancelable: true });
    document.querySelector("#guard").dispatchEvent(accepted);
    expect(accepted.defaultPrevented).toBe(false);
    const plain = new Event("submit", { bubbles: true, cancelable: true });
    document.querySelector("#plain").dispatchEvent(plain);
    expect(plain.defaultPrevented).toBe(false);
    expect(confirm).toHaveBeenNthCalledWith(1, "정말?");
  });
});

describe("tabs, global actions and resizing", () => {
  test("switches left and right tabs, updates ARIA and focuses search", async () => {
    shellPage();
    await boot();
    document.querySelector('.sb-tab[data-tab="search"]').click();
    expect(document.querySelector('.sb-tab[data-tab="search"]').getAttribute("aria-selected")).toBe("true");
    expect(document.querySelector('[data-panel="files"]').hidden).toBe(true);
    expect(document.activeElement).toBe(document.querySelector("#sb-search-input"));
    document.querySelector('.rp-tab[data-rp="links"]').click();
    expect(document.querySelector('.rp-tab[data-rp="outline"]').getAttribute("aria-selected")).toBe("false");
    expect(document.querySelector('[data-rp-panel="links"]').hidden).toBe(false);
    expect(document.querySelector('.rp-tab[data-rp="links"]').tabIndex).toBe(0);
    expect(document.querySelector('.rp-tab[data-rp="outline"]').tabIndex).toBe(-1);
  });

  test("provides roving tab focus with arrows, Home and End", async () => {
    shellPage();
    await boot();
    const files = document.querySelector('.sb-tab[data-tab="files"]');
    const search = document.querySelector('.sb-tab[data-tab="search"]');
    const press = (target, key) => {
      const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      target.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(true);
    };

    press(files, "ArrowRight");
    expect(document.activeElement).toBe(search);
    expect(search.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).not.toBe(document.querySelector("#sb-search-input"));
    press(search, "ArrowDown");
    expect(document.activeElement).toBe(files);
    press(files, "ArrowLeft");
    expect(document.activeElement).toBe(search);
    press(search, "ArrowUp");
    expect(document.activeElement).toBe(files);
    press(files, "End");
    expect(document.activeElement).toBe(search);
    press(search, "Home");
    expect(document.activeElement).toBe(files);
    const ordinary = new KeyboardEvent("keydown", { key: "x", bubbles: true, cancelable: true });
    files.dispatchEvent(ordinary);
    expect(ordinary.defaultPrevented).toBe(false);
  });

  test("opens search through the public API and handles absent tabs and inputs", async () => {
    shellPage({ search: false });
    const shell = await boot();
    shell.classList.add("no-left");
    window.WikiShell.openSearchTab();
    expect(shell.classList.contains("no-left")).toBe(false);
    window.WikiShell.openSearchTab();
    expect(shell.classList.contains("no-left")).toBe(false);
    document.querySelector(".sb-tabs").remove();
    shell.classList.add("no-left");
    window.WikiShell.openSearchTab();
    expect(shell.classList.contains("no-left")).toBe(false);
  });

  test("dispatches palette actions only when the palette API exists", async () => {
    shellPage();
    document.body.insertAdjacentHTML("beforeend", '<button id="palette" data-action="palette"><span></span></button><button id="switcher" data-action="switcher"></button><button id="unknown" data-action="unknown"></button>');
    const palette = { openCommands: vi.fn(), openSwitcher: vi.fn() };
    window.WikiPalette = palette;
    await boot();
    document.querySelector("#palette span").click();
    document.querySelector("#switcher").click();
    document.querySelector("#unknown").click();
    expect(palette.openCommands).toHaveBeenCalledOnce();
    expect(palette.openSwitcher).toHaveBeenCalledOnce();
    delete window.WikiPalette;
    document.querySelector("#palette").click();
    document.body.click();
    expect(document.querySelector(".ctx-menu")).toBeNull();
  });

  test("toggles the left panel with Ctrl and Cmd backslash", async () => {
    shellPage();
    const shell = await boot();
    const ctrl = new KeyboardEvent("keydown", { key: "\\", ctrlKey: true, bubbles: true, cancelable: true });
    document.dispatchEvent(ctrl);
    expect(ctrl.defaultPrevented).toBe(true);
    expect(shell.classList.contains("no-left")).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "\\", metaKey: true, bubbles: true }));
    expect(shell.classList.contains("no-left")).toBe(false);
  });

  test("resizes both panels with pointer capture fallback, clamps and persists widths", async () => {
    shellPage();
    document.documentElement.style.setProperty("--left-w", "200px");
    document.documentElement.style.setProperty("--right-w", "bad");
    await boot();
    const left = document.querySelector("#left-resizer");
    const right = document.querySelector("#right-resizer");
    left.setPointerCapture = vi.fn(() => { throw new Error("unsupported"); });
    right.setPointerCapture = vi.fn();
    expect(left.style.touchAction).toBe("none");
    expect(left.getAttribute("aria-valuenow")).toBe("200");
    expect(right.getAttribute("aria-valuenow")).toBe("300");

    const down = pointer("pointerdown", { pointerId: 1, clientX: 100 });
    left.dispatchEvent(down);
    expect(down.defaultPrevented).toBe(true);
    expect(document.body.classList.contains("resizing")).toBe(true);
    left.dispatchEvent(pointer("pointermove", { clientX: 1000 }));
    expect(document.documentElement.style.getPropertyValue("--left-w")).toBe("560px");
    expect(left.getAttribute("aria-valuetext")).toBe("560픽셀");
    left.dispatchEvent(pointer("pointerup", { clientX: 1000 }));
    expect(localStorage.getItem("wiki-left-w")).toBe("560");
    expect(document.body.classList.contains("resizing")).toBe(false);

    right.dispatchEvent(pointer("pointerdown", { pointerId: 2, clientX: 400 }));
    right.dispatchEvent(pointer("pointermove", { clientX: 1000 }));
    expect(document.documentElement.style.getPropertyValue("--right-w")).toBe("150px");
    right.dispatchEvent(pointer("pointercancel", { clientX: 1000 }));
    expect(localStorage.getItem("wiki-right-w")).toBe("150");
    right.dispatchEvent(pointer("pointermove", { clientX: 0 }));
    expect(document.documentElement.style.getPropertyValue("--right-w")).toBe("150px");

    document.documentElement.style.removeProperty("--left-w");
    left.dispatchEvent(pointer("pointerdown", { pointerId: 3, clientX: 100 }));
    left.dispatchEvent(pointer("pointermove", { clientX: -1000 }));
    expect(document.documentElement.style.getPropertyValue("--left-w")).toBe("150px");
    left.dispatchEvent(pointer("pointerup", { clientX: -1000 }));
  });

  test("resizes separators from the keyboard with physical arrow direction", async () => {
    shellPage();
    document.documentElement.style.setProperty("--left-w", "200px");
    document.documentElement.style.setProperty("--right-w", "300px");
    await boot();
    const left = document.querySelector("#left-resizer");
    const right = document.querySelector("#right-resizer");
    const press = (target, key) => {
      const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      target.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(true);
    };

    press(left, "ArrowRight");
    expect(left.getAttribute("aria-valuenow")).toBe("210");
    press(left, "ArrowLeft");
    expect(left.getAttribute("aria-valuenow")).toBe("200");
    press(left, "Home");
    expect(document.documentElement.style.getPropertyValue("--left-w")).toBe("150px");
    press(left, "End");
    expect(localStorage.getItem("wiki-left-w")).toBe("560");

    press(right, "ArrowLeft");
    expect(right.getAttribute("aria-valuenow")).toBe("310");
    press(right, "ArrowRight");
    expect(right.getAttribute("aria-valuenow")).toBe("300");
    const ordinary = new KeyboardEvent("keydown", { key: "x", bubbles: true, cancelable: true });
    right.dispatchEvent(ordinary);
    expect(ordinary.defaultPrevented).toBe(false);
  });
});

describe("file tree state and refresh", () => {
  test("restores valid open state, saves toggles, reveals and collapses the active document", async () => {
    localStorage.setItem("wiki-tree-open", JSON.stringify(["docs"]));
    shellPage({ tree: folder("docs", doc('a\\&quot;b.md', "Old")) + doc("stale.md") });
    document.body.insertAdjacentHTML("beforeend", '<span id="rt-meta" data-path="a\\&quot;b.md"></span><button id="collapse" data-action="collapse-all"></button>');
    vi.stubGlobal("CSS", undefined);
    await boot();
    const details = document.querySelector("details");
    expect(details.open).toBe(true);
    expect(document.querySelector('[data-doc="stale.md"]').classList.contains("active")).toBe(false);
    details.open = false;
    details.dispatchEvent(new Event("toggle"));
    expect(JSON.parse(localStorage.getItem("wiki-tree-open"))).toEqual([]);
    document.querySelector("#collapse").click();
    expect(details.open).toBe(false);
  });

  test("survives malformed open state and absent tree, metadata path and matching links", async () => {
    localStorage.setItem("wiki-tree-open", "{");
    shellPage({ tree: folder("docs", doc("else.md")) });
    await boot();
    expect(document.querySelector("details").open).toBe(false);
    document.querySelector("#file-tree").remove();
    document.querySelector("[data-action=collapse-all]")?.click();
    expect(window.WikiShell).toBeDefined();
  });

  test("initializes without a tree and safely collapses after a tree is removed", async () => {
    shellPage();
    document.querySelector("#file-tree").remove();
    document.body.insertAdjacentHTML("beforeend", '<button id="collapse-missing" data-action="collapse-all"></button>');
    await boot();
    document.querySelector("#collapse-missing").click();
    expect(window.WikiShell).toBeDefined();
  });

  test("removes stale active state before returning for an absent or unmatched path", async () => {
    shellPage({ tree: doc("old.md") });
    document.querySelector(".tree-doc").classList.add("active");
    await boot();
    expect(document.querySelector(".tree-doc").classList.contains("active")).toBe(false);

    shellPage({ tree: doc("other.md") });
    document.body.insertAdjacentHTML("beforeend", '<span id="rt-meta" data-path="missing.md"></span>');
    await boot();
    expect(document.querySelector(".tree-doc").classList.contains("active")).toBe(false);
  });

  test("refreshes escaped writable tree markup and keeps active ancestors open", async () => {
    shellPage();
    document.body.insertAdjacentHTML("beforeend", '<span id="rt-meta" data-path="dir/a b.md"></span>');
    vi.stubGlobal("CSS", { escape: (value) => value.replace(" ", "\\ ") });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ json: () => Promise.resolve({
      ok: true,
      tree: { folders: [{ name: '<A&"\'>', path: "dir", folders: [], docs: [{ title: '<T&"\'>', path: "dir/a b.md" }] }], docs: [] },
    }) }));
    await boot();
    await window.WikiShell.refreshTree();
    const tree = document.querySelector("#file-tree");
    expect(tree.textContent).toContain('<A&"\'>');
    expect(tree.textContent).toContain('<T&"\'>');
    expect(tree.querySelector(".tree-add").getAttribute("data-folder")).toBe("dir");
    expect(tree.querySelector(".tree-doc").getAttribute("href")).toBe("/doc/dir/a%20b.md");
    expect(tree.querySelector(".tree-doc").classList.contains("active")).toBe(true);
    expect(tree.querySelector("details").open).toBe(true);
  });

  test("renders writable and read-only empty states and ignores invalid, missing and failed refreshes", async () => {
    shellPage();
    const fetch = vi.fn()
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: true, tree: { folders: [], docs: [] } }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: false }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve(null) })
      .mockRejectedValueOnce(new Error("offline"));
    vi.stubGlobal("fetch", fetch);
    await boot();
    await window.WikiShell.refreshTree();
    expect(document.querySelector("#file-tree").textContent).toContain("첫 노트");
    const html = document.querySelector("#file-tree").innerHTML;
    await window.WikiShell.refreshTree();
    await window.WikiShell.refreshTree();
    await window.WikiShell.refreshTree();
    expect(document.querySelector("#file-tree").innerHTML).toBe(html);
    document.querySelector("#file-tree").remove();
    fetch.mockResolvedValueOnce({ json: () => Promise.resolve({ ok: true, tree: { folders: [], docs: [] } }) });
    await window.WikiShell.refreshTree();

    shellPage();
    fetch.mockResolvedValueOnce({ json: () => Promise.resolve({ ok: true, tree: { folders: [], docs: [] } }) });
    await boot({}, { canWrite: false, csrf: "" });
    await window.WikiShell.refreshTree();
    expect(document.querySelector("#file-tree").textContent).not.toContain("첫 노트");
  });

  test("renders missing folder and document arrays plus read-only folders", async () => {
    shellPage();
    const fetch = vi.fn()
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: true, tree: {} }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: true, tree: { folders: [{ name: "Only", path: "only" }] } }) });
    vi.stubGlobal("fetch", fetch);
    await boot({}, { canWrite: false, csrf: "" });
    await window.WikiShell.refreshTree();
    expect(document.querySelector("#file-tree").textContent).toContain("아직 문서가 없습니다");
    await window.WikiShell.refreshTree();
    expect(document.querySelector("#file-tree .tree-add")).toBeNull();
    expect(document.querySelector("#file-tree details").getAttribute("data-folder")).toBe("only");
  });
});

describe("inline creation and tree menus", () => {
  test("creates a root folder, posts CSRF data, refreshes and removes its toast", async () => {
    vi.useFakeTimers();
    shellPage();
    document.body.insertAdjacentHTML("beforeend", '<button id="new-folder" data-action="new-folder"></button>');
    const fetch = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true, path: "notes" }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: true, tree: { folders: [], docs: [] } }) });
    vi.stubGlobal("fetch", fetch);
    await boot();
    document.querySelector("#new-folder").click();
    const input = document.querySelector(".tree-inline-input");
    input.value = " notes ";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    input.dispatchEvent(new Event("blur"));
    await finishRequest();
    const [url, request] = fetch.mock.calls[0];
    expect(url).toBe("/api/folders");
    expect(request.headers).toEqual({ "X-CSRF-Token": "token" });
    expect(request.body.get("path")).toBe("notes");
    expect(request.body.get("csrf_token")).toBe("token");
    expect(document.querySelector(".rt-toast").textContent).toBe("폴더 생성: notes");
    vi.runAllTimers();
    expect(document.querySelector(".rt-toast")).toBeNull();
  });

  test("cancels and commits inline folder input through Escape, blank blur and blur", async () => {
    shellPage();
    document.body.insertAdjacentHTML("beforeend", '<button id="new-folder" data-action="new-folder"></button>');
    const fetch = vi.fn().mockResolvedValue({ ok: false, json: () => Promise.resolve({ error: { message: "중복" } }) });
    vi.stubGlobal("fetch", fetch);
    await boot();
    const button = document.querySelector("#new-folder");
    button.click();
    document.querySelector(".tree-inline-input").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(document.querySelector(".tree-input-row")).toBeNull();
    button.click();
    document.querySelector(".tree-inline-input").dispatchEvent(new Event("blur"));
    expect(fetch).not.toHaveBeenCalled();
    button.click();
    document.querySelector(".tree-inline-input").value = "dup";
    document.querySelector(".tree-inline-input").dispatchEvent(new Event("blur"));
    await finishRequest();
    expect(document.querySelector(".rt-toast").textContent).toBe("폴더 생성 실패: 중복");
  });

  test("opens folder and root context menus, positions them and cycles keyboard focus", async () => {
    shellPage({ tree: folder("docs") });
    await boot();
    const row = document.querySelector("summary");
    row.getBoundingClientRect = () => ({ left: 20, bottom: 30 });
    row.focus();
    const open = new KeyboardEvent("keydown", { key: "F10", shiftKey: true, bubbles: true, cancelable: true });
    row.dispatchEvent(open);
    expect(open.defaultPrevented).toBe(true);
    const menu = document.querySelector(".ctx-menu");
    expect(menu.getAttribute("role")).toBe("menu");
    expect(menu.style.left).toBe("28px");
    expect(document.activeElement.textContent).toBe("새 문서");
    menu.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
    expect(document.activeElement.textContent).toBe("새 문서");
    menu.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement.textContent).toBe("새 하위 폴더");
    menu.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
    expect(document.activeElement.textContent).toBe("새 문서");
    menu.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
    expect(document.activeElement.textContent).toBe("빈 폴더 삭제");
    menu.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement.textContent).toBe("새 문서");
    menu.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.querySelector(".ctx-menu")).toBeNull();
    expect(document.activeElement).toBe(row);

    const tree = document.querySelector("#file-tree");
    tree.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: innerWidth, clientY: innerHeight }));
    expect([...document.querySelectorAll(".ctx-item")].map((el) => el.textContent)).toEqual(["새 문서", "새 폴더"]);
    expect(parseInt(document.querySelector(".ctx-menu").style.left, 10)).toBeLessThan(innerWidth);
    document.dispatchEvent(new Event("scroll"));
    expect(document.querySelector(".ctx-menu")).toBeNull();
  });

  test("starts document creation from global, folder, inline and root-menu actions", async () => {
    shellPage({ tree: folder("docs") });
    document.body.insertAdjacentHTML("beforeend", '<button id="new-doc" data-action="new-doc"></button>');
    document.querySelector("summary").insertAdjacentHTML("beforeend", '<button id="new-here" data-action="new-doc-here" data-folder="docs"></button>');
    vi.stubGlobal("location", { href: "" });
    await boot();

    document.querySelector("#new-doc").click();
    let input = document.querySelector(".tree-inline-input");
    expect(document.querySelector(".tree-twisty-leaf")).not.toBeNull();
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Other", bubbles: true }));
    input.value = "root note";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(location.href).toBe("/new?path=root%20note");
    expect(document.querySelector(".tree-inline-input")).toBeNull();

    const here = new MouseEvent("click", { bubbles: true, cancelable: true });
    document.querySelector("#new-here").dispatchEvent(here);
    expect(here.defaultPrevented).toBe(true);
    input = document.querySelector(".tree-inline-input");
    expect(input.previousElementSibling.textContent).toBe("docs/");
    input.value = "child";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(location.href).toBe("/new?path=docs%2Fchild");

    document.querySelector("summary").dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    document.querySelector(".ctx-item").click();
    input = document.querySelector(".tree-inline-input");
    input.value = "menu child";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(location.href).toBe("/new?path=docs%2Fmenu%20child");

    document.querySelector("#file-tree").dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    document.querySelectorAll(".ctx-item")[1].click();
    input = document.querySelector(".tree-inline-input");
    expect(input.placeholder).toBe("폴더 이름");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    document.querySelector("#file-tree").dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    document.querySelector(".ctx-item").click();
    input = document.querySelector(".tree-inline-input");
    input.value = "menu root";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(location.href).toBe("/new?path=menu%20root");
  });

  test("falls back to the tree when a requested folder or child container is missing", async () => {
    shellPage({ tree: '<details class="tree-folder" data-folder="bare"><summary class="tree-row tree-folder-row" data-folder="bare"></summary></details>' });
    document.querySelector("summary").insertAdjacentHTML("beforeend", '<button id="ghost-doc" data-action="new-doc-here" data-folder="ghost"></button>');
    await boot();
    document.querySelector("#ghost-doc").click();
    expect(document.querySelector("#file-tree > .tree-input-row")).not.toBeNull();
    document.querySelector(".tree-inline-input").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    document.querySelector("summary").dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    document.querySelectorAll(".ctx-item")[1].click();
    expect(document.querySelector("#file-tree > .tree-input-row")).not.toBeNull();
    expect(document.querySelector(".tree-add-prefix").textContent).toBe("bare/");

    document.querySelector("#file-tree").remove();
    document.body.insertAdjacentHTML("beforeend", '<button id="orphan-new" data-action="new-doc"></button>');
    document.querySelector("#orphan-new").click();
    expect(document.querySelector(".tree-inline-input")).toBeNull();
  });

  test("renames and deletes documents through observable requests and confirmation", async () => {
    vi.useFakeTimers();
    shellPage({ tree: doc("old.md") });
    const fetch = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true, path: "new.md" }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: false }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: false }) });
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(window, "prompt").mockReturnValue(" new.md ");
    vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await boot();
    const row = document.querySelector(".tree-doc");
    row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    document.querySelector(".ctx-item").click();
    await finishRequest();
    expect(fetch.mock.calls[0][0]).toBe("/api/doc/old.md/move");
    expect(fetch.mock.calls[0][1].body.get("new_path")).toBe("new.md");
    expect(document.querySelector(".rt-toast").textContent).toBe("이동: new.md");

    row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    document.querySelector(".ctx-item.danger").click();
    expect(fetch).toHaveBeenCalledTimes(2);
    row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    document.querySelector(".ctx-item.danger").click();
    await finishRequest();
    expect(fetch.mock.calls[2][0]).toBe("/doc/old.md/delete");
    expect([...document.querySelectorAll(".rt-toast")].at(-1).textContent).toBe("삭제: old.md");
  });

  test("handles unchanged rename and failed move error shapes", async () => {
    shellPage({ tree: doc("old.md") });
    const fetch = vi.fn().mockResolvedValue({ ok: false, json: () => Promise.resolve({ error: "거부" }) });
    vi.stubGlobal("fetch", fetch);
    const prompt = vi.spyOn(window, "prompt").mockReturnValueOnce(null).mockReturnValueOnce(" old.md ").mockReturnValueOnce("other.md");
    await boot();
    for (let i = 0; i < 3; i += 1) {
      document.querySelector(".tree-doc").dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
      document.querySelector(".ctx-item").click();
    }
    await finishRequest();
    expect(prompt).toHaveBeenCalledTimes(3);
    expect(fetch.mock.calls[0][1].body.get("new_path")).toBe("other.md");
    expect(document.querySelector(".rt-toast").textContent).toBe("이동 실패: 거부");
  });

  test("creates inside folders and reports folder deletion success and errors", async () => {
    vi.useFakeTimers();
    shellPage({ tree: folder("docs") });
    const fetch = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true, path: "docs/sub" }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: false }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: false }) })
      .mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ message: "비어 있지 않음" }) });
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await boot();
    const row = document.querySelector("summary");
    row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    document.querySelectorAll(".ctx-item")[1].click();
    const input = document.querySelector(".tree-inline-input");
    expect(document.querySelector("details").open).toBe(true);
    expect(document.querySelector(".tree-add-prefix").textContent).toBe("docs/");
    input.value = "sub";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await finishRequest();
    expect(fetch.mock.calls[0][1].body.get("path")).toBe("docs/sub");

    row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    document.querySelector(".ctx-item.danger").click();
    await finishRequest();
    expect(fetch.mock.calls[2][0]).toBe("/api/folders/docs/delete");
    expect([...document.querySelectorAll(".rt-toast")].at(-1).textContent).toBe("폴더 삭제: docs");
    row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    document.querySelector(".ctx-item.danger").click();
    await finishRequest();
    expect([...document.querySelectorAll(".rt-toast")].at(-1).textContent).toBe("삭제 실패: 비어 있지 않음");
  });

  test("cancels folder deletion and uses the generic error fallback", async () => {
    shellPage({ tree: folder("docs") });
    const fetch = vi.fn().mockResolvedValue({ ok: false, json: () => Promise.resolve(null) });
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await boot();
    const row = document.querySelector("summary");
    row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    document.querySelector(".ctx-item.danger").click();
    expect(fetch).not.toHaveBeenCalled();

    document.body.insertAdjacentHTML("beforeend", '<button id="new-folder-fallback" data-action="new-folder"></button>');
    document.querySelector("#new-folder-fallback").click();
    const input = document.querySelector(".tree-inline-input");
    input.value = "broken";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await finishRequest();
    expect(document.querySelector(".rt-toast").textContent).toBe("폴더 생성 실패: 오류");
  });

  test("ignores context-menu keyboard shortcuts away from rows and handles text targets", async () => {
    shellPage({ tree: "plain text" });
    await boot();
    const tree = document.querySelector("#file-tree");
    const ignored = new KeyboardEvent("keydown", { key: "F10", shiftKey: true, bubbles: true, cancelable: true });
    tree.dispatchEvent(ignored);
    expect(ignored.defaultPrevented).toBe(false);
    tree.dispatchEvent(new KeyboardEvent("keydown", { key: "A", bubbles: true }));
    tree.firstChild.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    expect([...document.querySelectorAll(".ctx-item")].map((item) => item.textContent)).toEqual(["새 문서", "새 폴더"]);
  });

  test("keeps context menus disabled for viewers and ignores keyboard invocation off rows", async () => {
    shellPage({ tree: doc("read.md") });
    await boot({}, { canWrite: false, csrf: "" });
    const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    document.querySelector(".tree-doc").dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(document.querySelector(".ctx-menu")).toBeNull();
  });
});

describe("sidebar search", () => {
  test("clears blank queries and ignores stale successful results", async () => {
    vi.useFakeTimers();
    shellPage();
    let resolve;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((done) => { resolve = done; })));
    await boot();
    const input = document.querySelector("#sb-search-input");
    const out = document.querySelector("#sb-search-results");
    input.value = "old";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    expect(out.getAttribute("aria-busy")).toBe("true");
    vi.advanceTimersByTime(150);
    input.value = "new";
    resolve({ json: () => Promise.resolve({ ok: true, items: [{ title: "Old", path: "old.md" }] }) });
    await finishRequest();
    expect(out.textContent).toBe("검색 중…");
    input.value = "   ";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    expect(out.innerHTML).toBe("");
    expect(out.hasAttribute("aria-busy")).toBe(false);
  });

  test("renders encoded escaped results, empty and invalid responses", async () => {
    vi.useFakeTimers();
    shellPage();
    const fetch = vi.fn()
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: true, items: [{ title: '<T&"\'>', path: "dir/a b.md" }] }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: true, items: [] }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve({ ok: false }) })
      .mockResolvedValueOnce({ json: () => Promise.resolve(null) });
    vi.stubGlobal("fetch", fetch);
    await boot();
    const input = document.querySelector("#sb-search-input");
    const out = document.querySelector("#sb-search-results");
    for (const q of ["한 글", "empty", "bad", "null"]) {
      input.value = q;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await vi.advanceTimersByTimeAsync(150);
      await finishRequest();
      if (q === "한 글") {
        expect(fetch.mock.calls[0][0]).toBe("/api/complete?q=%ED%95%9C%20%EA%B8%80");
        expect(out.querySelector("a").href).toContain("/doc/dir/a%20b.md");
        expect(out.textContent).toBe('<T&"\'>dir/a b.md');
      } else expect(out.textContent).toBe("결과 없음");
      expect(out.hasAttribute("aria-busy")).toBe(false);
    }
  });

  test("shows only current request failures and tolerates missing search elements", async () => {
    vi.useFakeTimers();
    shellPage();
    let reject;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve, fail) => { reject = fail; })));
    await boot();
    const input = document.querySelector("#sb-search-input");
    const out = document.querySelector("#sb-search-results");
    input.value = "old";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    vi.advanceTimersByTime(150);
    input.value = "new";
    reject(new Error("offline"));
    await finishRequest();
    expect(out.textContent).toBe("검색 중…");

    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    vi.advanceTimersByTime(150);
    await finishRequest();
    expect(out.textContent).toBe("검색 실패");
    expect(out.hasAttribute("aria-busy")).toBe(false);

    shellPage({ search: false });
    await boot();
    expect(window.WikiShell).toBeDefined();
  });
});
