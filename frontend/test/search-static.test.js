import { afterEach, describe, expect, test, vi } from "vitest";
import { loadStatic, useStaticIsolation } from "./static-test-utils.js";

useStaticIsolation();

afterEach(() => history.replaceState(null, "", "/"));

function page() {
  document.body.innerHTML = `
    <section id="search-workbench">
      <form id="search-remove-form">
        <input name="q" value='free tag:release tag:todo tag:release title:"API guide"'>
        <input name="mode" value="bm25">
        <input name="folder" value="notes">
        <input name="tag" value="release">
        <input name="tag" value="todo">
        <input name="tag" value="release">
        <input name="page" value="8">
        <input name="per_page" value="10">
      </form>
      <input id="search-query" type="search">
      <details id="search-help"><summary>검색 연산자 도움말</summary><p>help</p></details>
      <button type="button" class="filter-chip" data-remove-filter="query"
        data-filter-operator="tag" data-filter-value="release" data-filter-index="2">remove second</button>
      <button type="button" class="filter-chip" data-remove-filter="tag"
        data-request-tag="release" data-filter-index="0">remove request tag</button>
      <details class="result-metadata"><summary>메타데이터</summary><p tabindex="-1">details</p></details>
    </section>`;
}

describe("search.js", () => {
  test("waits for readiness and skips pages without the workbench", async () => {
    vi.spyOn(document, "readyState", "get").mockReturnValue("loading");
    await loadStatic("search");
    expect(window.WikiSearch).toBeDefined();
    document.dispatchEvent(new Event("DOMContentLoaded"));
    expect(window.WikiSearch).toBeDefined();
  });

  test("removes only the selected duplicate query filter and resets the page", async () => {
    page();
    const form = document.querySelector("form");
    form.requestSubmit = vi.fn();
    await loadStatic("search");

    document.querySelector('[data-remove-filter="query"]').click();

    expect(form.elements.q.value).toBe('free tag:release tag:todo title:"API guide"');
    expect([...form.querySelectorAll('[name="tag"]')].map((input) => input.value)).toEqual([
      "release", "todo", "release",
    ]);
    expect(form.elements.folder.value).toBe("notes");
    expect(form.elements.mode.value).toBe("bm25");
    expect(form.elements.per_page.value).toBe("10");
    expect(form.elements.page.value).toBe("1");
    expect(form.requestSubmit).toHaveBeenCalledOnce();
  });

  test("preserves free-text and quoted-filter whitespace byte for byte", async () => {
    page();
    const form = document.querySelector("form");
    form.elements.q.value = 'free   words tag:release title:"API   guide"  tail';
    form.requestSubmit = vi.fn();
    document.querySelector('[data-remove-filter="query"]').dataset.filterIndex = "0";
    await loadStatic("search");

    document.querySelector('[data-remove-filter="query"]').click();

    expect(form.elements.q.value).toBe('free   words title:"API   guide"  tail');
    expect(form.requestSubmit).toHaveBeenCalledOnce();
  });

  test("uses server token order for adjacent quoted operators", async () => {
    page();
    const form = document.querySelector("form");
    form.elements.q.value = 'free title:"A"tag:x tag:x';
    form.requestSubmit = vi.fn();
    document.querySelector('[data-remove-filter="query"]').dataset.filterIndex = "0";
    await loadStatic("search");

    document.querySelector('[data-remove-filter="query"]').click();

    expect(form.elements.q.value).toBe("free tag:x tag:x");
    expect(form.requestSubmit).toHaveBeenCalledOnce();
  });

  test("skips empty quoted and malformed bare operators before a valid filter", async () => {
    page();
    const form = document.querySelector("form");
    form.elements.q.value = 'needle title:"" path: has:"   " tag:release tag:todo tag:release tail';
    form.requestSubmit = vi.fn();
    document.querySelector('[data-remove-filter="query"]').dataset.filterIndex = "0";
    await loadStatic("search");

    document.querySelector('[data-remove-filter="query"]').click();

    expect(form.elements.q.value).toBe(
      'needle title:"" path: has:"   " tag:todo tag:release tail',
    );
    expect(form.requestSubmit).toHaveBeenCalledOnce();
  });

  test("removes the exact duplicate after empty operators between valid filters", async () => {
    page();
    const form = document.querySelector("form");
    form.elements.q.value = 'needle tag:release title:"  " tag:todo title: tag:release tail';
    form.requestSubmit = vi.fn();
    document.querySelector('[data-remove-filter="query"]').dataset.filterIndex = "2";
    await loadStatic("search");

    document.querySelector('[data-remove-filter="query"]').click();

    expect(form.elements.q.value).toBe(
      'needle tag:release title:"  " tag:todo title: tail',
    );
    expect(form.elements.mode.value).toBe("bm25");
    expect(form.elements.folder.value).toBe("notes");
    expect([...form.querySelectorAll('[name="tag"]')]).toHaveLength(3);
    expect(form.elements.page.value).toBe("1");
    expect(form.elements.per_page.value).toBe("10");
    expect(form.requestSubmit).toHaveBeenCalledOnce();
  });

  test("removes one repeated request tag without changing query or other state", async () => {
    page();
    const form = document.querySelector("form");
    form.requestSubmit = vi.fn();
    await loadStatic("search");

    document.querySelector('[data-remove-filter="tag"]').click();

    expect([...form.querySelectorAll('[name="tag"]')].map((input) => input.value)).toEqual([
      "todo", "release",
    ]);
    expect(form.elements.q.value).toContain("tag:release tag:todo tag:release");
    expect(form.elements.page.value).toBe("1");
    expect(form.requestSubmit).toHaveBeenCalledOnce();
  });

  test("removes the folder and ignores stale or unrelated removal controls", async () => {
    page();
    const root = document.querySelector("#search-workbench");
    const form = document.querySelector("form");
    form.requestSubmit = vi.fn();
    root.insertAdjacentHTML("beforeend", `
      <button type="button" data-remove-filter="folder">folder</button>
      <button type="button" data-remove-filter="query" data-filter-index="99">stale query</button>
      <button type="button" data-remove-filter="tag" data-filter-index="99">stale tag</button>
      <button type="button" data-remove-filter="unknown">unknown</button>
      <span id="unrelated">plain</span>`);
    await loadStatic("search");

    root.querySelector('[data-remove-filter="folder"]').click();
    expect(form.querySelector('[name="folder"]')).toBeNull();
    expect(form.requestSubmit).toHaveBeenCalledOnce();
    root.querySelector('[data-remove-filter="folder"]').click();
    expect(form.requestSubmit).toHaveBeenCalledOnce();
    for (const selector of [
      '[data-remove-filter="query"][data-filter-index="99"]',
      '[data-remove-filter="tag"][data-filter-index="99"]',
      '[data-remove-filter="unknown"]',
      "#unrelated",
    ]) root.querySelector(selector).click();
    expect(form.requestSubmit).toHaveBeenCalledOnce();

    document.querySelector("#search-remove-form").remove();
    root.querySelector('[data-remove-filter="query"]').click();
    expect(form.requestSubmit).toHaveBeenCalledOnce();
  });

  test("opens help from the keyboard and Escape closes details with focus restored", async () => {
    page();
    await loadStatic("search");
    const help = document.querySelector("#search-help");
    const helpSummary = help.querySelector("summary");
    const metadata = document.querySelector(".result-metadata");
    const metadataSummary = metadata.querySelector("summary");

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "?", bubbles: true }));
    expect(help.open).toBe(true);
    expect(document.activeElement).toBe(helpSummary);

    metadata.open = true;
    metadata.querySelector("p").focus();
    metadata.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(metadata.open).toBe(false);
    expect(document.activeElement).toBe(metadataSummary);
  });

  test("does not steal shortcuts from editable controls and fully disposes behavior", async () => {
    page();
    await loadStatic("search");
    const help = document.querySelector("#search-help");
    const input = document.querySelector("#search-query");
    input.focus();
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "?", bubbles: true }));
    expect(help.open).toBe(false);

    const controller = window.WikiSearch;
    controller.dispose();
    expect(window.WikiSearch).toBeUndefined();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "?", bubbles: true }));
    expect(help.open).toBe(false);
    controller.dispose();
  });

  test("ignores unavailable help, unrelated keys and Escape outside open workbench details", async () => {
    page();
    document.querySelector("#search-help").remove();
    await loadStatic("search");
    const root = document.querySelector("#search-workbench");
    root.dispatchEvent(new KeyboardEvent("keydown", { key: "?", bubbles: true }));
    root.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    root.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    const outside = document.createElement("details");
    outside.open = true;
    outside.innerHTML = "<summary>outside</summary><button>target</button>";
    document.body.appendChild(outside);
    outside.querySelector("button").dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    expect(outside.open).toBe(true);
  });

  test("reloading disposes the previous controller and isolates the new one", async () => {
    page();
    await loadStatic("search");
    const first = window.WikiSearch;
    const dispose = vi.spyOn(first, "dispose");
    await loadStatic("search");
    expect(dispose).toHaveBeenCalledOnce();
    expect(window.WikiSearch).not.toBe(first);
  });

  test("disposes pending readiness and tolerates stale globals and missing roots", async () => {
    vi.spyOn(document, "readyState", "get").mockReturnValue("loading");
    window.WikiSearch = {};
    await loadStatic("search");
    const pending = window.WikiSearch;
    const ready = document.addEventListener.mock.calls.find(([type]) => type === "DOMContentLoaded")[1];
    pending.dispose();
    document.body.innerHTML = '<section id="search-workbench"></section>';
    ready();
    document.dispatchEvent(new Event("DOMContentLoaded"));
    expect(window.WikiSearch).toBeUndefined();

    vi.restoreAllMocks();
    await loadStatic("search");
    const missing = window.WikiSearch;
    window.WikiSearch = { replacement: true };
    missing.dispose();
    expect(window.WikiSearch).toEqual({ replacement: true });
  });
});
