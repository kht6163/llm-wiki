import { describe, expect, test, vi } from "vitest";
import { flush, loadStatic, useStaticIsolation } from "./static-test-utils.js";

useStaticIsolation();

function markup(rows = true) {
  return `
    <section id="doc-props-wrap" data-path="folder/a b.md" data-version="12">
      ${rows ? `<div class="doc-props">
        <div class="prop" data-key="status"><span class="prop-chip">draft</span><span class="prop-chip">review</span></div>
        <div class="prop" data-key="owner"><span class="prop-chip">Kim</span></div>
      </div>` : '<div class="doc-props"></div>'}
      <button data-action="edit-props"><span>속성 편집</span></button>
    </section>`;
}

async function boot(rows = true) {
  document.body.innerHTML = markup(rows);
  window.WIKI = { canWrite: true, csrf: "csrf" };
  await loadStatic("props");
  return document.querySelector("#doc-props-wrap");
}

function openEditor() {
  document.querySelector('[data-action="edit-props"] span').click();
  return document.querySelector(".props-editor");
}

describe("props.js", () => {
  test("does not initialize without an editable property panel", async () => {
    delete window.WIKI;
    await loadStatic("props");
    window.WIKI = { canWrite: true };
    await loadStatic("props");
    document.body.innerHTML = markup();
    window.WIKI = { canWrite: false };
    await loadStatic("props");
    document.querySelector('[data-action="edit-props"]').click();
    expect(document.querySelector(".props-editor")).toBeNull();
  });

  test("edits current rows, adds/removes rows and cancels cleanly", async () => {
    const wrap = await boot();
    const editor = openEditor();
    expect(wrap.classList.contains("editing")).toBe(true);
    expect([...editor.querySelectorAll(".pe-key")].map((e) => e.value)).toEqual(["status", "owner"]);
    expect([...editor.querySelectorAll(".pe-val")].map((e) => e.value)).toEqual(["draft, review", "Kim"]);
    expect(editor.querySelector(".pe-key")).toBe(document.activeElement);
    expect(editor.querySelector(".pe-key").getAttribute("aria-label")).toBe("속성 이름");
    expect(editor.querySelector(".pe-val").getAttribute("aria-label")).toBe("속성 값");
    expect(editor.querySelector(".pe-rm").title).toBe("이 속성 삭제");

    document.querySelector('[data-action="edit-props"]').click();
    expect(document.querySelectorAll(".props-editor")).toHaveLength(1);
    editor.querySelector(".pe-rm").click();
    expect(editor.querySelectorAll(".pe-row")).toHaveLength(1);
    editor.querySelector(".pe-add").click();
    const added = editor.querySelectorAll(".pe-row")[1];
    expect(added.querySelector(".pe-key")).toBe(document.activeElement);
    expect(added.querySelector(".pe-key").placeholder).toBe("속성");
    expect(added.querySelector(".pe-val").placeholder).toBe("값 (여러 개는 쉼표로)");

    const cancel = editor.querySelector(".pe-cancel");
    cancel.click();
    expect(wrap.classList.contains("editing")).toBe(false);
    expect(document.querySelector(".props-editor")).toBeNull();
    cancel.click();
  });

  test("posts trimmed non-empty properties and reloads after success", async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) }));
    vi.stubGlobal("fetch", fetchMock);
    const reload = vi.fn();
    vi.stubGlobal("location", { reload });
    await boot();
    const editor = openEditor();
    editor.querySelectorAll(".pe-key")[0].value = "  stage  ";
    editor.querySelectorAll(".pe-val")[0].value = "one, two";
    editor.querySelectorAll(".pe-key")[1].value = "   ";
    editor.querySelector(".pe-save").click();
    expect(editor.querySelector(".pe-save").disabled).toBe(true);
    await flush();
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/doc/folder/a%20b.md/properties");
    expect(options).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf" },
      credentials: "same-origin",
    });
    expect(JSON.parse(options.body)).toEqual({ base_version: 12, properties: [{ key: "stage", values: "one, two" }] });
    expect(reload).toHaveBeenCalledOnce();
  });

  test("restores save state and reports every server error shape", async () => {
    const replies = [
      { ok: false, d: { error: { message: "conflict" } }, message: "conflict" },
      { ok: false, d: { error: "denied" }, message: "denied" },
      { ok: false, d: { message: "bad" }, message: "bad" },
      { ok: false, d: {}, message: "오류" },
    ];
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    for (const reply of replies) {
      vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: reply.ok, json: () => Promise.resolve(reply.d) })));
      await boot(false);
      const editor = openEditor();
      expect(editor.querySelector(".pe-key")).toBeNull();
      editor.querySelector(".pe-add").click();
      editor.querySelector(".pe-save").click();
      await flush();
      expect(editor.querySelector(".pe-save").disabled).toBe(false);
      expect(alert).toHaveBeenLastCalledWith(`저장 실패: ${reply.message}`);
    }
  });

  test("restores save state after a network error", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    await boot(false);
    const editor = openEditor();
    editor.querySelector(".pe-save").click();
    await flush();
    expect(editor.querySelector(".pe-save").disabled).toBe(false);
    expect(alert).toHaveBeenCalledWith("저장 실패");
  });
});
