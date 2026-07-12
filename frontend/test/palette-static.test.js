import { beforeEach, describe, expect, test, vi } from "vitest";
import { flush, loadStatic, useStaticIsolation } from "./static-test-utils.js";

let requests;

useStaticIsolation();

beforeEach(() => {
  vi.useFakeTimers();
  requests = [];
  vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve, reject) => requests.push({ resolve, reject }))));
  vi.stubGlobal("location", { href: "" });
  Element.prototype.scrollIntoView = vi.fn();
});

function page() {
  document.body.innerHTML = `
    <button id="outside">outside</button>
    <div id="cmd-overlay" hidden>
      <input id="cmd-input">
      <ul id="cmd-list"></ul>
    </div>`;
}

async function boot(permissions = {}) {
  page();
  window.WIKI = { canWrite: false, canAdmin: false, ...permissions };
  window.WikiShell = {
    toggleLeft: vi.fn(),
    toggleRight: vi.fn(),
    toggleTheme: vi.fn(),
  };
  await loadStatic("palette");
  return {
    overlay: document.querySelector("#cmd-overlay"),
    input: document.querySelector("#cmd-input"),
    list: document.querySelector("#cmd-list"),
  };
}

function key(target, key, init = {}) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
}

function type(input, value) {
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function chooseLabel(list, label, event = "mousedown") {
  const row = [...list.querySelectorAll(".cmd-item")].find((el) => el.querySelector(".cmd-label").textContent === label);
  row.dispatchEvent(new MouseEvent(event, { bubbles: true, cancelable: true }));
  return row;
}

describe("palette.js", () => {
  test("requires the complete palette DOM", async () => {
    await loadStatic("palette");
    document.body.innerHTML = '<div id="cmd-overlay"></div>';
    await loadStatic("palette");
    document.body.innerHTML = '<div id="cmd-overlay"></div><input id="cmd-input">';
    await loadStatic("palette");
    expect(window.WikiPalette).toBeUndefined();
  });

  test("filters commands by permissions and fuzzy subsequences", async () => {
    const { input, list } = await boot();
    window.WikiPalette.openCommands();
    expect(document.querySelector("#cmd-overlay").hidden).toBe(false);
    expect(input.placeholder).toBe("명령 입력…");
    expect(input.getAttribute("aria-expanded")).toBe("true");
    expect(list.textContent).not.toContain("새 문서 만들기");
    expect(list.textContent).not.toContain("활동 피드");
    expect(list.textContent).not.toContain("사용자 관리");

    type(input, "gp");
    expect([...list.querySelectorAll(".cmd-label")].map((el) => el.textContent)).toEqual(["그래프 보기", "우측 패널 토글"]);
    type(input, "not-present");
    expect(list.textContent).toBe("결과 없음");
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);

    const writable = await boot({ canWrite: true, canAdmin: true });
    window.WikiPalette.openCommands();
    expect(writable.list.textContent).toContain("새 문서 만들기");
    expect(writable.list.textContent).toContain("활동 피드");
    expect(writable.list.textContent).toContain("사용자 관리");
  });

  test("runs every static command through its visible behavior", async () => {
    const { list } = await boot({ canWrite: true, canAdmin: true });
    const destinations = new Map([
      ["새 문서 만들기", "/new"], ["문서 목록", "/"], ["그래프 보기", "/graph"],
      ["활동 피드", "/activity"], ["태그 보기", "/tags"], ["깨진 링크", "/broken-links"],
      ["검색 페이지", "/search"], ["API 키 / 설정", "/settings"],
      ["사용자 관리", "/admin/users"], ["로그아웃", "/logout"],
    ]);
    for (const [label, href] of destinations) {
      window.WikiPalette.openCommands();
      chooseLabel(list, label);
      expect(location.href).toBe(href);
    }
    for (const [label, method] of [["좌측 사이드바 토글", "toggleLeft"], ["우측 패널 토글", "toggleRight"], ["라이트/다크 테마 전환", "toggleTheme"]]) {
      window.WikiPalette.openCommands();
      chooseLabel(list, label);
      expect(window.WikiShell[method]).toHaveBeenCalledOnce();
    }

    delete window.WikiShell.toggleLeft;
    window.WikiPalette.openCommands();
    chooseLabel(list, "좌측 사이드바 토글");
    expect(document.querySelector("#cmd-overlay").hidden).toBe(true);
    window.WikiShell = {};
    for (const label of ["우측 패널 토글", "라이트/다크 테마 전환"]) {
      window.WikiPalette.openCommands();
      chooseLabel(list, label);
      expect(document.querySelector("#cmd-overlay").hidden).toBe(true);
    }
    delete window.WikiShell;
    window.WikiPalette.openCommands();
    chooseLabel(list, "좌측 사이드바 토글");
  });

  test("supports pointer selection, bounded arrow navigation, enter, tab and escape", async () => {
    const { overlay, input, list } = await boot();
    document.querySelector("#outside").focus();
    window.WikiPalette.openCommands();
    vi.runOnlyPendingTimers();
    expect(document.activeElement).toBe(input);

    const down = key(input, "ArrowDown");
    expect(down.defaultPrevented).toBe(true);
    expect(list.children[1].classList.contains("active")).toBe(true);
    expect(list.children[1].getAttribute("aria-selected")).toBe("true");
    key(input, "ArrowUp");
    key(input, "ArrowUp");
    expect(list.children[0].classList.contains("active")).toBe(true);
    list.lastElementChild.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
    expect(input.getAttribute("aria-activedescendant")).toBe(list.lastElementChild.id);
    key(input, "ArrowDown");
    expect(list.lastElementChild.classList.contains("active")).toBe(true);

    expect(key(input, "Tab").defaultPrevented).toBe(true);
    expect(key(input, "x").defaultPrevented).toBe(false);
    key(input, "Escape");
    expect(overlay.hidden).toBe(true);
    expect(document.activeElement).toBe(document.querySelector("#outside"));
    key(input, "Escape");

    window.WikiPalette.openCommands();
    key(input, "Enter");
    expect(location.href).toBe("/");
  });

  test("closes from backdrop and safely ignores a vanished focus target", async () => {
    const { overlay } = await boot();
    const outside = document.querySelector("#outside");
    outside.focus();
    outside.focus = vi.fn(() => { throw new Error("gone"); });
    window.WikiPalette.openCommands();
    overlay.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(overlay.hidden).toBe(true);
    overlay.firstElementChild?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  });

  test("opens and toggles both global shortcuts without hijacking other keys", async () => {
    const { overlay, input } = await boot();
    expect(key(document, "x", { ctrlKey: true }).defaultPrevented).toBe(false);
    expect(key(document, "p", { ctrlKey: true, shiftKey: true }).defaultPrevented).toBe(false);
    expect(key(document, "p", { ctrlKey: true }).defaultPrevented).toBe(true);
    expect(input.placeholder).toBe("명령 입력…");
    key(document, "p", { metaKey: true });
    expect(overlay.hidden).toBe(true);
    key(document, "o", { metaKey: true });
    expect(input.placeholder).toBe("문서 이름/경로로 이동…");
    key(document, "o", { ctrlKey: true });
    expect(overlay.hidden).toBe(true);
  });

  test("renders switcher loading, results, encoded navigation and creation", async () => {
    const { input, list } = await boot({ canWrite: true });
    window.WikiPalette.openSwitcher();
    type(input, "   ");
    expect(list.textContent).toBe("결과 없음");
    type(input, "a b");
    expect(list.textContent).toBe("검색 중…");
    expect(list.firstElementChild.getAttribute("aria-hidden")).toBe("true");
    key(input, "ArrowUp");
    key(input, "ArrowDown");
    key(input, "Enter");
    vi.advanceTimersByTime(130);
    expect(fetch).toHaveBeenCalledWith("/api/complete?q=a%20b", { credentials: "same-origin" });
    requests[0].resolve({ json: () => Promise.resolve({ ok: true, items: [{ title: "Doc", path: "folder/a b.md" }] }) });
    await flush();
    expect([...list.querySelectorAll(".cmd-label")].map((el) => el.textContent)).toEqual(["Doc", "새 문서: a b"]);
    expect(list.querySelector(".cmd-sub").textContent).toBe("folder/a b.md");
    chooseLabel(list, "Doc");
    expect(location.href).toBe("/doc/folder/a%20b.md");

    window.WikiPalette.openSwitcher();
    type(input, "new/name");
    vi.advanceTimersByTime(130);
    requests[1].resolve({ json: () => Promise.resolve({ ok: false }) });
    await flush();
    chooseLabel(list, "새 문서: new/name");
    expect(location.href).toBe("/new?path=new%2Fname");
  });

  test("ignores stale switcher replies and clears current network failures", async () => {
    const { input, list } = await boot();
    window.WikiPalette.openSwitcher();
    type(input, "old");
    vi.advanceTimersByTime(130);
    type(input, "new");
    requests[0].resolve({ json: () => Promise.resolve({ ok: true, items: [{ title: "Old", path: "old.md" }] }) });
    await flush();
    expect(list.textContent).toBe("검색 중…");
    vi.advanceTimersByTime(130);
    requests[1].reject(new Error("offline"));
    await flush();
    expect(list.textContent).toBe("결과 없음");

    type(input, "gone");
    vi.advanceTimersByTime(130);
    type(input, "newer");
    requests[2].reject(new Error("offline"));
    await flush();
    expect(list.textContent).toBe("검색 중…");
  });
});
