import { describe, expect, test, vi } from "vitest";
import { flush, loadStatic, useStaticIsolation } from "./static-test-utils.js";

useStaticIsolation();

async function loadWith(html, fetchImpl, readyState = "complete") {
  document.body.innerHTML = html;
  vi.spyOn(document, "readyState", "get").mockReturnValue(readyState);
  vi.stubGlobal("fetch", vi.fn(fetchImpl));
  await loadStatic("activity");
  return document.querySelector("#rp-activity");
}

describe("activity.js", () => {
  test("skips missing placeholders and paths", async () => {
    await loadWith("", () => Promise.resolve());
    expect(fetch).not.toHaveBeenCalled();
    await loadWith('<div id="rp-activity"></div>', () => Promise.resolve());
    expect(fetch).not.toHaveBeenCalled();
  });

  test("waits for DOM readiness and renders via badges with localized timestamps", async () => {
    window.WikiLocalizeTime = vi.fn();
    const box = await loadWith(
      '<div id="rp-activity" data-path="folder/a b.md" aria-live="polite"></div>',
      () => Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          ok: true,
          events: [
            {
              ts: "2026-07-16T12:00:00Z",
              actor: "alice",
              via: "mcp",
              action: "doc_update",
              target: "folder/a b.md",
              outcome: "ok",
              detail: "v2",
            },
            {
              ts: "2026-07-16T11:00:00Z",
              actor: "alice",
              via: "web",
              action: "doc_move",
              target: "old.md -> folder/a b.md",
              outcome: "ok",
              detail: null,
            },
          ],
        }),
      }),
      "loading",
    );
    expect(fetch).not.toHaveBeenCalled();
    document.dispatchEvent(new Event("DOMContentLoaded"));
    expect(box.textContent).toBe("활동 불러오는 중…");
    expect(box.className).toBe("rp-activity muted is-loading");
    expect(box.getAttribute("aria-busy")).toBe("true");
    await flush();
    expect(fetch).toHaveBeenCalledWith("/api/doc/folder/a%20b.md/activity", {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    expect(box.getAttribute("aria-busy")).toBeNull();
    expect(box.getAttribute("aria-live")).toBe("polite");
    const items = box.querySelectorAll(".rp-activity-list > li");
    expect(items).toHaveLength(2);
    expect(items[0].classList.contains("via-mcp-row")).toBe(true);
    expect(items[0].querySelector(".via-badge.via-mcp").textContent).toBe("에이전트");
    expect(items[0].querySelector(".rp-activity-action").textContent).toBe("문서 수정");
    expect(items[0].querySelector(".rp-activity-actor").textContent).toBe("alice");
    expect(items[0].querySelector(".rp-activity-detail").textContent).toBe("v2");
    expect(items[0].querySelector("time.dt").getAttribute("datetime")).toBe(
      "2026-07-16T12:00:00Z",
    );
    expect(items[1].querySelector(".via-badge.via-web").textContent).toBe("사람");
    expect(items[1].querySelector(".rp-activity-action").textContent).toBe("문서 이동");
    expect(items[1].querySelector(".rp-activity-target").textContent).toBe(
      "old.md -> folder/a b.md",
    );
    expect(window.WikiLocalizeTime).toHaveBeenCalledWith(box);
  });

  test("shows muted empty or error states for empty/HTTP/network failures", async () => {
    for (const [factory, message] of [
      [
        () => Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, events: [] }) }),
        "이 문서의 활동이 없습니다",
      ],
      [() => Promise.resolve({ ok: false }), "활동을 불러오지 못했습니다"],
      [() => Promise.reject(new Error("offline")), "활동을 불러오지 못했습니다"],
      [
        () => Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: false }) }),
        "활동을 불러오지 못했습니다",
      ],
      [
        () => Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ ok: true }), // no events field
        }),
        "이 문서의 활동이 없습니다",
      ],
    ]) {
      const box = await loadWith(
        '<div id="rp-activity" data-path="a.md" aria-live="polite"></div>',
        factory,
      );
      await flush();
      expect(box.className).toBe("rp-activity muted");
      expect(box.textContent).toBe(message);
      expect(box.hasAttribute("aria-busy")).toBe(false);
      vi.restoreAllMocks();
    }
  });

  test("renders unknown via, missing timestamp, non-ok outcome, and bare action labels", async () => {
    const box = await loadWith(
      '<div id="rp-activity" data-path="x.md"></div>',
      () => Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          ok: true,
          events: [
            {
              ts: null,
              actor: "",
              via: "batch",
              action: "custom_op",
              target: "x.md",
              outcome: "error",
              detail: "",
            },
            {
              ts: "2026-07-16T10:00:00Z",
              actor: "bob",
              via: "cli",
              action: "doc_create",
              target: "x.md",
              outcome: "ok",
            },
            {
              ts: "2026-07-16T09:00:00Z",
              actor: "carol",
              via: null,
              action: null,
              target: "x.md",
              outcome: "ok",
              detail: null,
            },
          ],
        }),
      }),
    );
    await flush();
    const items = box.querySelectorAll(".rp-activity-list > li");
    expect(items).toHaveLength(3);
    expect(items[0].querySelector(".via-badge").textContent).toBe("batch");
    expect(items[0].querySelector(".via-badge").classList.contains("via-batch")).toBe(false);
    expect(items[0].querySelector("time.dt").textContent).toBe("—");
    expect(items[0].querySelector(".rp-activity-action").textContent).toBe("custom_op");
    expect(items[0].querySelector(".outcome-bad").textContent).toBe("error");
    expect(items[0].querySelector(".rp-activity-actor")).toBeNull();
    expect(items[1].querySelector(".via-badge.via-cli").textContent).toBe("CLI");
    // Null via/action: no badge, action falls back to em dash.
    expect(items[2].querySelector(".via-badge")).toBeNull();
    expect(items[2].querySelector(".rp-activity-action").textContent).toBe("—");
  });
});
