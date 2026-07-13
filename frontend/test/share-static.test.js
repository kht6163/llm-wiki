import { beforeEach, describe, expect, test, vi } from "vitest";
import { flush, loadStatic, useStaticIsolation } from "./static-test-utils.js";

useStaticIsolation();

function page(path = "folder/shared note.md") {
  document.body.innerHTML = `
    <button id="share-toggle" aria-expanded="false"></button>
    <section id="share-panel" data-share-path="${path}" hidden>
      <button id="share-generate"></button>
      <div id="share-result" hidden>
        <input id="share-url">
        <button id="share-copy"></button>
      </div>
      <p id="share-status" class="muted"></p>
      <button id="share-refresh"></button>
      <p id="share-list-empty" hidden></p>
      <ul id="share-links"></ul>
    </section>`;
}

async function boot({ wiki = { csrf: "csrf-token" }, path } = {}) {
  page(path);
  if (wiki) window.WIKI = wiki;
  await loadStatic("share");
  return {
    toggle: document.querySelector("#share-toggle"),
    panel: document.querySelector("#share-panel"),
    generate: document.querySelector("#share-generate"),
    result: document.querySelector("#share-result"),
    input: document.querySelector("#share-url"),
    copy: document.querySelector("#share-copy"),
    status: document.querySelector("#share-status"),
    refresh: document.querySelector("#share-refresh"),
    empty: document.querySelector("#share-list-empty"),
    list: document.querySelector("#share-links"),
  };
}

beforeEach(() => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  document.execCommand = vi.fn();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ ok: true, links: [] }),
  }));
});

describe("share.js", () => {
  test("does nothing without the required controls", async () => {
    await loadStatic("share");
    document.body.innerHTML = '<button id="share-toggle"></button>';
    await loadStatic("share");
    expect(window.WikiShare).toBeUndefined();
  });

  test("opens, closes and Escape-dismisses the disclosure", async () => {
    const ui = await boot();
    ui.toggle.click();
    expect(ui.panel.hidden).toBe(false);
    expect(ui.toggle.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(ui.generate);
    await flush();
    expect(ui.empty.hidden).toBe(false);
    ui.toggle.click();
    expect(ui.panel.hidden).toBe(true);

    ui.toggle.click();
    const escape = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    ui.panel.dispatchEvent(escape);
    expect(escape.defaultPrevented).toBe(true);
    expect(ui.panel.hidden).toBe(true);
    expect(document.activeElement).toBe(ui.toggle);
    const ordinary = new KeyboardEvent("keydown", { key: "x", bubbles: true, cancelable: true });
    ui.panel.dispatchEvent(ordinary);
    expect(ordinary.defaultPrevented).toBe(false);
  });

  test("mints an encoded 30-day link with CSRF and reports completion", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ ok: true, url: "https://wiki.test/share/signed" }),
    });
    vi.stubGlobal("fetch", fetch);
    const ui = await boot();
    ui.generate.click();
    expect(ui.generate.disabled).toBe(true);
    expect(ui.generate.getAttribute("aria-busy")).toBe("true");
    await flush();
    expect(fetch).toHaveBeenCalledWith(
      "/api/doc/folder/shared%20note.md/share",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
    const options = fetch.mock.calls[0][1];
    expect(options.headers["X-CSRF-Token"]).toBe("csrf-token");
    expect(options.body.get("csrf_token")).toBe("csrf-token");
    expect(ui.input.value).toBe("https://wiki.test/share/signed");
    expect(ui.result.hidden).toBe(false);
    expect(ui.status.textContent).toContain("30일 후 만료");
    expect(ui.status.classList.contains("share-error")).toBe(false);
    expect(ui.generate.disabled).toBe(false);
    expect(ui.generate.hasAttribute("aria-busy")).toBe(false);
  });

  test("surfaces API and transport failures, including the default message", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ error: { message: "denied" } }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: false, error: "invalid" }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: false }) })
      .mockRejectedValueOnce({})
      .mockRejectedValueOnce(new Error("offline"));
    vi.stubGlobal("fetch", fetch);
    const ui = await boot({ wiki: null, path: "plain.md" });

    for (const message of ["denied", "invalid", "요청을 처리하지 못했습니다.",
      "공유 링크를 만들지 못했습니다.", "offline"]) {
      ui.generate.click();
      await flush();
      expect(ui.status.textContent).toBe(message);
      expect(ui.status.classList.contains("share-error")).toBe(true);
      expect(ui.generate.disabled).toBe(false);
    }
    expect(fetch.mock.calls[0][1].headers["X-CSRF-Token"]).toBe("");
  });

  test("loads and refreshes active, expired and revoked ledger rows", async () => {
    const links = [
      { id: 1, created_at: "2026-01-01T00:00:00Z", expires_at: "2999-01-01T00:00:00Z", revoked_at: null, last_used_at: "2026-01-02T00:00:00Z", created_by_name: "alice" },
      { id: 2, created_at: "2026-01-01T00:00:00Z", expires_at: "2000-01-01T00:00:00Z", revoked_at: null, last_used_at: null, created_by_name: null },
      { id: 3, created_at: "2026-01-01T00:00:00Z", expires_at: "2999-01-01T00:00:00Z", revoked_at: "2026-01-03T00:00:00Z", last_used_at: null, created_by_name: "admin" },
      { id: 4, created_at: "2026-01-01T00:00:00Z", expires_at: "invalid", revoked_at: null, last_used_at: null, created_by_name: "alice" },
    ];
    const fetch = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true, links }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true }) });
    vi.stubGlobal("fetch", fetch);
    window.WikiLocalizeTime = vi.fn();
    const ui = await boot();
    ui.toggle.click();
    await flush();

    expect(fetch.mock.calls[0]).toEqual([
      "/api/doc/folder/shared%20note.md/shares",
      { credentials: "same-origin" },
    ]);
    expect([...ui.list.querySelectorAll(".share-state")].map((item) => item.textContent))
      .toEqual(["활성", "만료", "취소", "활성"]);
    expect(ui.list.querySelectorAll(".share-revoke")).toHaveLength(2);
    expect(ui.list.textContent).toContain("마지막 사용");
    expect(ui.list.textContent).toContain("발급자 알 수 없음");
    expect(window.WikiLocalizeTime).toHaveBeenCalledWith(ui.list);
    expect(ui.refresh.disabled).toBe(false);
    expect(ui.list.hasAttribute("aria-busy")).toBe(false);

    ui.toggle.click();
    ui.toggle.click();
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);
    ui.refresh.click();
    await flush();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(ui.empty.hidden).toBe(false);
  });

  test("uses mint metadata to prepend and deduplicate the ledger", async () => {
    const oldLinks = [
      { id: 1, created_at: "2026-01-01T00:00:00Z", expires_at: "2999-01-01T00:00:00Z", revoked_at: null, last_used_at: null, created_by_name: "alice" },
      { id: 2, created_at: "2026-01-01T00:00:00Z", expires_at: "2999-01-01T00:00:00Z", revoked_at: null, last_used_at: null, created_by_name: "alice" },
    ];
    const replacement = { ...oldLinks[0], created_at: "2026-02-01T00:00:00Z" };
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true, links: oldLinks }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true, url: "https://wiki/share/new", link: replacement }) }));
    const ui = await boot();
    ui.toggle.click();
    await flush();
    ui.generate.click();
    await flush();
    expect([...ui.list.children].map((item) => item.dataset.linkId)).toEqual(["1", "2"]);
    expect(ui.list.firstElementChild.textContent).toContain("2026-02-01");
  });

  test("revokes an active link and handles irrelevant or failed actions", async () => {
    const active = { id: 7, created_at: "2026-01-01T00:00:00Z", expires_at: "2999-01-01T00:00:00Z", revoked_at: null, last_used_at: null, created_by_name: "alice" };
    const fetch = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true, links: [active] }) })
      .mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ error: { message: "cannot revoke" } }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true, id: 7, revoked_at: "2026-02-01T00:00:00Z" }) });
    vi.stubGlobal("fetch", fetch);
    const ui = await boot();
    ui.toggle.click();
    await flush();

    ui.list.click();
    const text = document.createTextNode("plain");
    ui.list.appendChild(text);
    text.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const missing = document.createElement("button");
    missing.className = "share-revoke";
    missing.dataset.linkId = "999";
    ui.list.appendChild(missing);
    missing.click();
    expect(fetch).toHaveBeenCalledTimes(1);

    let revoke = ui.list.querySelector('[data-link-id="7"].share-revoke');
    revoke.click();
    await flush();
    expect(ui.status.textContent).toBe("cannot revoke");
    expect(revoke.disabled).toBe(false);
    expect(revoke.hasAttribute("aria-busy")).toBe(false);
    revoke.click();
    await flush();
    expect(fetch.mock.calls[2][0]).toBe("/api/shares/7/revoke");
    expect(ui.list.querySelector(".share-state").textContent).toBe("취소");
    expect(ui.list.querySelector(".share-revoke")).toBeNull();
    expect(ui.status.textContent).toContain("#7을 취소했습니다");
  });

  test("reports ledger loading failures and recovers through refresh", async () => {
    const fetch = vi.fn()
      .mockRejectedValueOnce({})
      .mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ ok: false }) });
    vi.stubGlobal("fetch", fetch);
    const ui = await boot();
    ui.toggle.click();
    await flush();
    expect(ui.status.textContent).toBe("발급 내역을 불러오지 못했습니다.");
    ui.refresh.click();
    await flush();
    expect(ui.status.textContent).toBe("요청을 처리하지 못했습니다.");
    expect(ui.refresh.disabled).toBe(false);
  });

  test("copies through Clipboard and reports a rejected write", async () => {
    const writeText = vi.fn()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("blocked"));
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const ui = await boot();
    ui.input.value = "https://wiki.test/share/token";
    ui.copy.click();
    await flush();
    expect(writeText).toHaveBeenCalledWith(ui.input.value);
    expect(ui.status.textContent).toContain("복사했습니다");
    ui.copy.click();
    await flush();
    expect(ui.status.textContent).toContain("직접 복사하세요");
    expect(document.activeElement).toBe(ui.input);
  });

  test("uses the selection fallback and handles a failed legacy copy", async () => {
    document.execCommand.mockReturnValueOnce(true).mockReturnValueOnce(false);
    const ui = await boot();
    ui.input.value = "https://wiki.test/share/token";
    const select = vi.spyOn(ui.input, "select");
    ui.copy.click();
    await flush();
    expect(document.execCommand).toHaveBeenCalledWith("copy");
    expect(ui.status.textContent).toContain("복사했습니다");
    ui.copy.click();
    await flush();
    expect(select).toHaveBeenCalledTimes(3);
    expect(ui.status.textContent).toContain("직접 복사하세요");
  });
});
