import { beforeEach, describe, expect, test, vi } from "vitest";
import { flush, loadStatic, useStaticIsolation } from "./static-test-utils.js";

let pending;

useStaticIsolation();

beforeEach(() => {
  vi.useFakeTimers();
  pending = [];
  vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve, reject) => pending.push({ resolve, reject }))));
  vi.stubGlobal("scrollX", 10);
  vi.stubGlobal("scrollY", 20);
});

function hover(el) {
  el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
  vi.advanceTimersByTime(250);
}

describe("preview.js", () => {
  test("shows loading, fetched, cached and empty previews at the link position", async () => {
    document.body.innerHTML = `
      <a class="title" href="/doc/folder/a%20b.md"><span>first</span></a>
      <a class="title" href="/doc/empty.md">empty</a>
      <a class="title" href="/doc/">root</a>
      <a class="title" href="/doc/remove.md">removed</a>
      <a class="title" href="/other">other</a>`;
    const [first, empty, root, removed, other] = document.querySelectorAll("a");
    first.getBoundingClientRect = () => ({ left: 4, bottom: 8 });
    empty.getBoundingClientRect = () => ({ left: 1, bottom: 2 });
    await loadStatic("preview");

    hover(first.querySelector("span"));
    const pop = document.querySelector(".doc-popover");
    expect(pop.textContent).toBe("불러오는 중…");
    expect(pop.hidden).toBe(false);
    expect(pop.style.left).toBe("14px");
    expect(pop.style.top).toBe("34px");
    expect(fetch).toHaveBeenCalledWith("/api/doc/folder/a%20b.md/preview");

    pending.shift().resolve({ json: () => Promise.resolve({ ok: true, title: "Title", excerpt: "Excerpt" }) });
    await flush();
    expect(pop.querySelector(".dp-title").textContent).toBe("Title");
    expect(pop.querySelector(".dp-excerpt").textContent).toBe("Excerpt");

    first.dispatchEvent(new MouseEvent("mouseout", { bubbles: true }));
    vi.advanceTimersByTime(200);
    expect(pop.hidden).toBe(true);
    hover(first);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(pop.hidden).toBe(false);

    hover(empty);
    pending.shift().resolve({ json: () => Promise.resolve({ ok: true }) });
    await flush();
    expect(pop.querySelector(".dp-title").textContent).toBe("");
    expect(pop.querySelector(".dp-excerpt").textContent).toBe("(내용 없음)");

    hover(root);
    removed.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
    removed.removeAttribute("href");
    vi.advanceTimersByTime(250);
    hover(other);
    document.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
    document.dispatchEvent(new MouseEvent("mouseout", { bubbles: true }));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  test("ignores stale and unsuccessful responses and survives network errors", async () => {
    document.body.innerHTML = `
      <a class="title" href="/doc/slow.md">slow</a>
      <a class="title" href="/doc/current.md">current</a>
      <a class="title" href="/doc/error.md">error</a>`;
    const [slow, current, error] = document.querySelectorAll("a");
    for (const link of [slow, current, error]) link.getBoundingClientRect = () => ({ left: 0, bottom: 0 });
    await loadStatic("preview");

    error.dispatchEvent(new MouseEvent("mouseout", { bubbles: true }));
    vi.advanceTimersByTime(200);
    hover(slow);
    hover(current);
    const pop = document.querySelector(".doc-popover");
    pending[0].resolve({ json: () => Promise.resolve({ ok: true, title: "stale", excerpt: "old" }) });
    await flush();
    expect(pop.textContent).toBe("불러오는 중…");
    pending[1].resolve({ json: () => Promise.resolve({ ok: false }) });
    await flush();
    expect(pop.textContent).toBe("불러오는 중…");

    hover(error);
    pending[2].reject(new Error("offline"));
    await flush();
    expect(pop.textContent).toBe("불러오는 중…");

    error.dispatchEvent(new MouseEvent("mouseout", { bubbles: true }));
    error.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
    vi.advanceTimersByTime(199);
    expect(pop.hidden).toBe(false);
  });
});
