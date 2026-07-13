import { beforeEach, describe, expect, test, vi } from "vitest";
import { flush, loadStatic, useStaticIsolation } from "./static-test-utils.js";

let sockets;

useStaticIsolation();

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.close = vi.fn();
    sockets.push(this);
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  sockets = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("requestAnimationFrame", (callback) => callback());
});

function page({ path = "folder/a b.md", mode = "view", version = "2", rendered = true, main = true } = {}) {
  document.body.innerHTML = `
    ${main ? "<main></main>" : ""}
    <div id="rt-meta" data-path="${path}" data-mode="${mode}" data-version="${version}"></div>
    ${rendered ? '<article class="rendered">old</article>' : ""}
    <input name="base_version" value="2"><input name="base_version" value="2">`;
}

async function boot(options, protocol = "http:") {
  page(options);
  vi.stubGlobal("location", { protocol, host: "wiki.test" });
  await loadStatic("realtime");
  return sockets.at(-1);
}

function message(ws, data) {
  ws.onmessage({ data: typeof data === "string" ? data : JSON.stringify(data) });
}

describe("realtime.js", () => {
  test("requires metadata, WebSocket support and a path outside list mode", async () => {
    document.body.innerHTML = "";
    await loadStatic("realtime");
    document.body.innerHTML = '<div id="rt-meta" data-path="a.md"></div>';
    vi.stubGlobal("WebSocket", undefined);
    await loadStatic("realtime");
    vi.stubGlobal("WebSocket", FakeWebSocket);
    page({ path: "", mode: "view" });
    await loadStatic("realtime");
    expect(sockets).toHaveLength(0);

    await boot({ path: "", mode: "list" });
    expect(sockets).toHaveLength(1);
  });

  test("connects with ws or wss and resets reconnect delay on open", async () => {
    const http = await boot();
    expect(http.url).toBe("ws://wiki.test/ws");
    http.onclose();
    vi.advanceTimersByTime(999);
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(2);

    const reconnected = sockets.at(-1);
    reconnected.onclose();
    vi.advanceTimersByTime(2000);
    const third = sockets.at(-1);
    third.onopen();
    third.onclose();
    vi.advanceTimersByTime(1000);
    expect(sockets).toHaveLength(4);

    sockets = [];
    const https = await boot({}, "https:");
    expect(https.url).toBe("wss://wiki.test/ws");
  });

  test("caps exponential reconnects and survives constructor failures", async () => {
    const ws = await boot();
    let current = ws;
    for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
      current.onclose();
      vi.advanceTimersByTime(delay);
      current = sockets.at(-1);
    }
    expect(sockets).toHaveLength(8);

    vi.stubGlobal("WebSocket", class { constructor() { throw new Error("blocked"); } });
    page();
    await loadStatic("realtime");
    expect(document.querySelector("#rt-banner")).toBeNull();
    window.dispatchEvent(new Event("beforeunload"));
  });

  test("ignores malformed, unrelated and stale messages", async () => {
    const ws = await boot();
    message(ws, "not json");
    message(ws, null);
    message(ws, { type: "presence" });
    message(ws, { type: "doc_changed", path: "other.md", op: "update", version: 3 });
    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "update", version: 2 });
    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "create", version: 1 });
    expect(document.querySelector("#rt-banner")).toBeNull();
  });

  test("shows list, delete and safely escaped move banners", async () => {
    let ws = await boot({ path: "", mode: "list" });
    message(ws, { type: "doc_changed", path: "anything.md" });
    expect(document.querySelector("#rt-banner").textContent).toContain("문서 목록에 변경");
    expect(document.querySelector("#rt-banner a").getAttribute("href")).toBe("");

    ws = await boot({ mode: "edit" });
    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "delete" });
    expect(document.querySelector("#rt-banner").className).toBe("rt-banner warn");
    expect(document.querySelector("#rt-banner a").getAttribute("href")).toBe("/");
    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "move", to: `new/<>&"'.md` });
    const link = document.querySelector("#rt-banner a");
    expect(link.textContent).toBe(`new/<>&"'.md`);
    expect(link.getAttribute("href")).toBe("/doc/new/%3C%3E%26%22'.md");
    expect(link.children).toHaveLength(0);
    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "move" });
    expect(document.querySelector("#rt-banner a").textContent).toBe("");
  });

  test("warns editors with all attribution fallbacks", async () => {
    const ws = await boot({ mode: "edit" });
    const cases = [
      [{ via: "mcp", updated_by: "<agent>" }, "에이전트(<agent>)"],
      [{ via: "cli" }, "CLI"],
      [{ via: "web", updated_by: "person" }, "사람(person)"],
      [{ via: "other", updated_by: "writer" }, "writer"],
      [{ via: "other" }, ""],
      [{ version: null }, ""],
    ];
    let version = 3;
    for (const [extra, expected] of cases) {
      message(ws, { type: "doc_changed", path: "folder/a b.md", op: "update", version: version++, ...extra });
      const banner = document.querySelector("#rt-banner");
      expect(banner.textContent).toContain("다른 곳에서");
      if (expected) expect(banner.textContent).toContain(expected);
      expect(banner.querySelector("agent")).toBeNull();
    }
  });

  test("refreshes a viewer, versions forms and reports event attribution", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ json: () => Promise.resolve({ ok: true, html: "<h1>new</h1>", version: 3, updated_by: "server" }) })));
    const ws = await boot();
    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "update", version: 3, via: "mcp", updated_by: "alice" });
    await flush();
    expect(fetch).toHaveBeenCalledWith("/api/doc/folder/a%20b.md/rendered", { credentials: "same-origin" });
    expect(document.querySelector(".rendered").innerHTML).toBe("<h1>new</h1>");
    expect(document.querySelector("#rt-meta").getAttribute("data-version")).toBe("3");
    expect([...document.querySelectorAll('input[name="base_version"]')].map((input) => input.value)).toEqual(["3", "3"]);
    const toast = document.querySelector(".rt-toast");
    expect(toast.textContent).toContain("에이전트(alice)");
    expect(toast.classList.contains("show")).toBe(true);
    vi.advanceTimersByTime(4500);
    expect(toast.classList.contains("show")).toBe(false);
    vi.advanceTimersByTime(400);
    expect(toast.isConnected).toBe(false);
  });

  test("uses server attribution and tolerates missing rendered content", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ json: () => Promise.resolve({ ok: true, html: "new", version: 4, updated_by: "server" }) })));
    const ws = await boot({ rendered: false, main: false });
    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "create", version: 4 });
    await flush();
    expect(document.querySelector(".rt-toast").textContent).toContain("server");
    expect(document.querySelector(".rendered")).toBeNull();

    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ json: () => Promise.resolve({ ok: true, html: "plain", version: 5 }) })));
    const unattributed = await boot({ rendered: false });
    message(unattributed, { type: "doc_changed", path: "folder/a b.md", op: "update", version: 5 });
    await flush();
    expect(document.querySelector(".rt-toast").textContent).toBe("문서가 v5(으)로 업데이트되었습니다");

    const editor = await boot({ mode: "edit", rendered: false, main: false });
    message(editor, { type: "doc_changed", path: "folder/a b.md", op: "update", version: 6 });
    expect(document.body.firstElementChild.id).toBe("rt-banner");
  });

  test("ignores unsuccessful refreshes and network errors", async () => {
    const responses = [
      Promise.resolve({ json: () => Promise.resolve(null) }),
      Promise.resolve({ json: () => Promise.resolve({ ok: false }) }),
      Promise.reject(new Error("offline")),
    ];
    vi.stubGlobal("fetch", vi.fn(() => responses.shift()));
    const ws = await boot();
    for (const version of [3, 4, 5]) {
      message(ws, { type: "doc_changed", path: "folder/a b.md", op: "update", version });
      await flush();
    }
    expect(document.querySelector(".rendered").textContent).toBe("old");
  });

  test("closes on socket errors and unload without reconnecting", async () => {
    const ws = await boot();
    ws.onerror();
    expect(ws.close).toHaveBeenCalledOnce();
    ws.close.mockImplementation(() => { throw new Error("already closed"); });
    ws.onerror();
    window.dispatchEvent(new Event("beforeunload"));
    ws.onclose();
    vi.runAllTimers();
    expect(sockets).toHaveLength(1);
  });

  test("cancels a scheduled reconnect on unload", async () => {
    const ws = await boot();
    ws.onclose();
    window.dispatchEvent(new Event("beforeunload"));
    vi.runAllTimers();
    expect(sockets).toHaveLength(1);
  });

  test("keeps late handlers from an old socket isolated from the current socket", async () => {
    const oldSocket = await boot();
    oldSocket.onclose();
    vi.advanceTimersByTime(1000);
    const currentSocket = sockets.at(-1);
    oldSocket.onerror();
    expect(oldSocket.close).toHaveBeenCalledOnce();
    expect(currentSocket.close).not.toHaveBeenCalled();
    oldSocket.onopen();
    message(oldSocket, { type: "doc_changed", path: "folder/a b.md", op: "delete" });
    oldSocket.onclose();
    expect(document.querySelector("#rt-banner")).toBeNull();
    vi.runAllTimers();
    expect(sockets).toHaveLength(2);
  });

  test("refuses a reconnect callback that races after unload", async () => {
    const timerSpy = vi.spyOn(globalThis, "setTimeout");
    const ws = await boot();
    ws.onclose();
    const reconnect = timerSpy.mock.calls.at(-1)[0];
    window.dispatchEvent(new Event("beforeunload"));
    reconnect();
    expect(sockets).toHaveLength(1);
  });

  test("soft-refreshes on focus/visibility when server version is newer", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      json: () => Promise.resolve({
        ok: true, html: "<h1>soft</h1>", version: 5, updated_by: "server", last_via: "mcp",
      }),
    })));
    await boot({ version: "2" });
    // Debounce: first tick within 2s should not fetch yet.
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(fetch).not.toHaveBeenCalled();
    vi.advanceTimersByTime(2000);
    await flush();
    expect(fetch).toHaveBeenCalledWith(
      "/api/doc/folder/a%20b.md/rendered",
      { credentials: "same-origin" },
    );
    expect(document.querySelector(".rendered").innerHTML).toBe("<h1>soft</h1>");
    expect(document.querySelector("#rt-meta").getAttribute("data-version")).toBe("5");
  });

  test("soft-refresh shows editor banner without replacing body", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      json: () => Promise.resolve({
        ok: true, html: "<p>ignored</p>", version: 9, updated_by: "bob", last_via: "web",
      }),
    })));
    await boot({ mode: "edit", version: "3", rendered: false });
    window.dispatchEvent(new Event("focus"));
    vi.advanceTimersByTime(2000);
    await flush();
    const banner = document.querySelector("#rt-banner");
    expect(banner).not.toBeNull();
    expect(banner.textContent).toContain("다른 곳에서");
    expect(banner.textContent).toContain("v9");
  });

  test("soft-refresh covers hidden, unattributed, and in-flight checks", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      json: () => Promise.resolve({ ok: true, html: "ignored", version: 4 }),
    })));
    await boot({ mode: "edit", version: "2" });

    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("focus"));
    expect(fetch).not.toHaveBeenCalled();

    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    window.dispatchEvent(new Event("focus"));
    vi.advanceTimersByTime(2000);
    await flush();
    expect(document.querySelector("#rt-banner").textContent).toContain("v4");
    expect(document.querySelector("#rt-banner").textContent).not.toContain(" · ");

    window.dispatchEvent(new Event("focus"));
    vi.advanceTimersByTime(2000);
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  test("soft-refresh skips list mode and absorbs network failures", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
    await boot({ path: "", mode: "list" });
    window.dispatchEvent(new Event("focus"));
    expect(fetch).not.toHaveBeenCalled();

    await boot();
    window.dispatchEvent(new Event("focus"));
    vi.advanceTimersByTime(2000);
    await flush();
    expect(fetch).toHaveBeenCalledOnce();
  });

  test("unload clears a pending soft-refresh and guards its racing callback", async () => {
    const timerSpy = vi.spyOn(globalThis, "setTimeout");
    vi.stubGlobal("fetch", vi.fn());
    await boot();
    window.dispatchEvent(new Event("focus"));
    const callback = timerSpy.mock.calls.at(-1)[0];
    window.dispatchEvent(new Event("beforeunload"));
    callback();
    expect(fetch).not.toHaveBeenCalled();
  });

  test("soft-refresh debounces and skips when version is not newer", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      json: () => Promise.resolve({ ok: true, html: "same", version: 2 }),
    })));
    await boot({ version: "2" });
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("focus"));
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(2000);
    await flush();
    // One coalesced check after debounce, no DOM refresh for non-newer version.
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".rendered").textContent).toBe("old");
    expect(document.querySelector("#rt-banner")).toBeNull();
  });

  test("does not let an older viewer response regress a newer rendered version", async () => {
    const pending = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve, reject) => pending.push({ resolve, reject }))));
    const ws = await boot();
    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "update", version: 3 });
    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "update", version: 4 });
    pending[1].resolve({ json: () => Promise.resolve({ ok: true, html: "<p>v4</p>", version: 4 }) });
    await flush();
    pending[0].resolve({ json: () => Promise.resolve({ ok: true, html: "<p>v3</p>", version: 3 }) });
    await flush();
    expect(document.querySelector(".rendered").innerHTML).toBe("<p>v4</p>");
    expect(document.querySelector("#rt-meta").getAttribute("data-version")).toBe("4");

    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "update", version: 5 });
    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "update", version: 6 });
    pending[3].resolve({ json: () => Promise.resolve({ ok: true, html: "<p>v6</p>", version: 6 }) });
    await flush();
    pending[2].reject(new Error("late failure"));
    await flush();
    expect(document.querySelector(".rendered").innerHTML).toBe("<p>v6</p>");
    expect(document.querySelector("#rt-meta").getAttribute("data-version")).toBe("6");

    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "update", version: 7 });
    pending[4].resolve({ json: () => Promise.resolve({ ok: true, html: "<p>old payload</p>", version: 5 }) });
    await flush();
    expect(document.querySelector(".rendered").innerHTML).toBe("<p>v6</p>");
    expect(document.querySelector("#rt-meta").getAttribute("data-version")).toBe("6");

    message(ws, { type: "doc_changed", path: "folder/a b.md", op: "update", version: 8 });
    pending[5].resolve({ json: () => Promise.resolve({ ok: true, html: "<p>missing version</p>" }) });
    await flush();
    expect(document.querySelector(".rendered").innerHTML).toBe("<p>v6</p>");
    expect(document.querySelector("#rt-meta").getAttribute("data-version")).toBe("6");
  });
});
