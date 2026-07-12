import { describe, expect, test, vi } from "vitest";
import { flush, loadStatic, useStaticIsolation } from "./static-test-utils.js";

useStaticIsolation();

async function loadWith(html, fetchImpl, readyState = "complete") {
  document.body.innerHTML = html;
  vi.spyOn(document, "readyState", "get").mockReturnValue(readyState);
  vi.stubGlobal("fetch", vi.fn(fetchImpl));
  await loadStatic("related");
  return document.querySelector("#rp-related");
}

describe("related.js", () => {
  test("skips missing placeholders and paths", async () => {
    await loadWith("", () => Promise.resolve());
    expect(fetch).not.toHaveBeenCalled();
    await loadWith('<div id="rp-related"></div>', () => Promise.resolve());
    expect(fetch).not.toHaveBeenCalled();
  });

  test("waits for DOM readiness and renders accessible encoded results", async () => {
    const box = await loadWith(
      '<div id="rp-related" data-path="folder/a b.md"></div>',
      () => Promise.resolve({ ok: true, json: () => Promise.resolve({
        related: [
          { path: "other/c d.md", title: "Other", score: 0.876 },
          { path: "fallback.md" },
        ],
      }) }),
      "loading",
    );
    expect(fetch).not.toHaveBeenCalled();
    document.dispatchEvent(new Event("DOMContentLoaded"));
    expect(box.textContent).toBe("관련 문서 불러오는 중…");
    expect(box.className).toBe("rp-related muted is-loading");
    expect(box.getAttribute("aria-busy")).toBe("true");
    await flush();
    expect(fetch).toHaveBeenCalledWith("/api/doc/folder/a%20b.md/related", { headers: { Accept: "application/json" } });
    expect(box.getAttribute("aria-busy")).toBeNull();
    expect(box.querySelector("h3").textContent).toBe("관련 문서");
    const links = box.querySelectorAll("a");
    expect([...links].map((a) => [a.href, a.textContent])).toEqual([
      ["http://localhost:3000/doc/other/c%20d.md", "Other"],
      ["http://localhost:3000/doc/fallback.md", "fallback.md"],
    ]);
    expect([...box.querySelectorAll(".sim")].map((s) => [s.title, s.textContent])).toEqual([
      ["유사도", "88%"], ["유사도", "0%"],
    ]);
  });

  test("clears loading state for empty, HTTP-error and network-error responses", async () => {
    for (const responseFactory of [
      () => Promise.resolve({ ok: true, json: () => Promise.resolve({ related: [] }) }),
      () => Promise.resolve({ ok: false }),
      () => Promise.reject(new Error("offline")),
    ]) {
      const box = await loadWith('<div id="rp-related" data-path="a.md"></div>', responseFactory);
      await flush();
      expect(box.className).toBe("rp-related");
      expect(box.textContent).toBe("");
      expect(box.hasAttribute("aria-busy")).toBe(false);
      vi.restoreAllMocks();
    }
  });
});
