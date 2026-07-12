import { afterEach, describe, expect, test, vi } from "vitest";
import { flush, loadStatic } from "./static-test-utils.js";

afterEach(() => vi.unstubAllGlobals());

async function setup(fetchImpl = vi.fn()) {
  document.body.innerHTML = `
    <div id="doc-rendered"><input type="checkbox" data-ti="3" disabled></div>
    <div id="rt-meta" data-path="folder/a b.md" data-version="7"></div>`;
  window.WIKI = { canWrite: true, csrf: "token" };
  const observers = [];
  vi.stubGlobal("MutationObserver", class {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe = vi.fn();
  });
  vi.stubGlobal("fetch", fetchImpl);
  await loadStatic("tasks");
  return { box: document.querySelector("input"), meta: document.querySelector("#rt-meta"), observer: observers[0] };
}

describe("tasks.js", () => {
  test("does nothing for readers or incomplete rendered views", async () => {
    delete window.WIKI;
    await loadStatic("tasks");
    window.WIKI = { canWrite: false };
    await loadStatic("tasks");
    expect(document.querySelector("input")).toBeNull();

    window.WIKI = { canWrite: true };
    await loadStatic("tasks");
    document.body.innerHTML = '<div id="doc-rendered"></div>';
    await loadStatic("tasks");
  });

  test("enables tasks, posts encoded state and accepts a new version", async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ json: () => Promise.resolve({ ok: true, version: 8 }) }));
    const { box, meta, observer } = await setup(fetchMock);
    expect(box.disabled).toBe(false);
    expect(observer.observe).toHaveBeenCalledWith(document.querySelector("#doc-rendered"), { childList: true });

    document.querySelector("#doc-rendered").dispatchEvent(new Event("change", { bubbles: true }));
    expect(fetchMock).not.toHaveBeenCalled();
    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    expect(box.disabled).toBe(true);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/doc/folder/a%20b.md/toggle-task");
    expect(options).toMatchObject({ method: "POST", headers: { "X-CSRF-Token": "token" }, credentials: "same-origin" });
    expect(Object.fromEntries(options.body)).toEqual({ index: "3", base_version: "7", csrf_token: "token" });
    expect(meta.dataset.version).toBe("8");

    document.querySelector("#doc-rendered").innerHTML = '<input type="checkbox" data-ti="4" disabled>';
    observer.callback();
    expect(document.querySelector("input").disabled).toBe(false);
  });

  test("rolls back failed API and network toggles", async () => {
    const responses = [
      Promise.resolve({ json: () => Promise.resolve({ ok: false }) }),
      Promise.reject(new Error("offline")),
    ];
    const { box, meta } = await setup(vi.fn(() => responses.shift()));
    meta.removeAttribute("data-version");

    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    expect(box.checked).toBe(false);
    expect(box.disabled).toBe(false);

    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    expect(box.checked).toBe(false);
    expect(box.disabled).toBe(false);
  });
});
