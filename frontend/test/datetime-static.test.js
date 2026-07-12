import { describe, expect, test, vi } from "vitest";
import { loadStatic, useStaticIsolation } from "./static-test-utils.js";

useStaticIsolation();

describe("datetime.js", () => {
  test("localizes valid timestamps once and supports inserted roots", async () => {
    vi.spyOn(document, "readyState", "get").mockReturnValue("complete");
    document.body.innerHTML = `
      <time class="dt" datetime="2026-01-02T03:04:05Z">UTC</time>
      <time class="dt" datetime="not-a-date">invalid</time>`;

    await loadStatic("datetime");

    const valid = document.querySelectorAll("time")[0];
    const expected = new Date("2026-01-02T03:04:05Z");
    const pad = (n) => String(n).padStart(2, "0");
    expect(valid.textContent).toBe(
      `${expected.getFullYear()}-${pad(expected.getMonth() + 1)}-${pad(expected.getDate())} ` +
      `${pad(expected.getHours())}:${pad(expected.getMinutes())}:${pad(expected.getSeconds())}`,
    );
    expect(valid.title).toBe("2026-01-02T03:04:05Z");
    expect(valid.dataset.localized).toBe("1");
    expect(document.querySelectorAll("time")[1].textContent).toBe("invalid");

    valid.textContent = "kept";
    window.WikiLocalizeTime();
    expect(valid.textContent).toBe("kept");

    const root = document.createElement("div");
    root.innerHTML = '<time class="dt" datetime="2026-11-12T13:14:15Z">UTC</time>';
    window.WikiLocalizeTime(root);
    expect(root.querySelector("time").dataset.localized).toBe("1");
  });

  test("waits for DOMContentLoaded while the document is loading", async () => {
    vi.spyOn(document, "readyState", "get").mockReturnValue("loading");
    document.body.innerHTML = '<time class="dt" datetime="2026-01-02T03:04:05Z">UTC</time>';
    await loadStatic("datetime");
    expect(document.querySelector("time").textContent).toBe("UTC");
    document.dispatchEvent(new Event("DOMContentLoaded"));
    expect(document.querySelector("time").dataset.localized).toBe("1");
  });
});
