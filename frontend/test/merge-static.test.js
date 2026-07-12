import { describe, expect, test, vi } from "vitest";
import { loadStatic, useStaticIsolation } from "./static-test-utils.js";

useStaticIsolation();

function payload(overrides = {}) {
  return {
    base: "head\nrepeat\nmiddle\nrepeat\ntail\n",
    mine: "head\nmine one\nmiddle\nmine two\ntail\n",
    current: "head\ncurrent one\nmiddle\ncurrent two\ntail\n",
    merged: "head\nrepeat\nmiddle\nrepeat\ntail\n",
    current_version: 7,
    manual_only: false,
    conflicts: [
      { start_line: 2, base: "repeat\n", mine: "mine one\n", current: "current one\n", resolved: null },
      { start_line: 4, base: "repeat\n", mine: "mine two\n", current: "current two\n", resolved: null },
    ],
    ...overrides,
  };
}

function conflictField(index, mine) {
  return `<fieldset class="merge-conflict" data-conflict-index="${index}">
    <button type="button" data-resolution="mine">mine</button>
    <button type="button" data-resolution="current">current</button>
    <textarea>${mine}</textarea>
    <button type="button" data-resolution="manual">manual</button>
  </fieldset>`;
}

function page(data = payload(), { state = "conflicts", editorApi, saveDisabled = true } = {}) {
  document.body.innerHTML = `
    <section id="merge-resolver" data-merge-state="${state}">
      <p id="merge-progress"></p>
      ${state === "conflicts" ? data.conflicts.map((h, i) => conflictField(i, h.mine)).join("") : ""}
      ${state === "proposal" ? '<button type="button" id="apply-merge-proposal">apply</button>' : ""}
    </section>
    <script id="merge-payload" type="application/json">${JSON.stringify(data).replaceAll("<", "\\u003c")}</script>
    <form class="editform"><input name="base_version" value="${data.current_version}">
      <textarea id="editor">${data.mine}</textarea><div id="md-editor-mount"></div>
      <button data-merge-save type="submit" ${saveDisabled ? "disabled" : ""}>save</button></form>`;
  if (editorApi !== undefined) document.querySelector("#md-editor-mount").wikiEditorApi = editorApi;
}

function submit(form) {
  const event = new Event("submit", { bubbles: true, cancelable: true });
  form.dispatchEvent(event);
  return event;
}

describe("merge.js", () => {
  test("blocks unresolved submission and resolves repeated hunks in source order", async () => {
    page();
    await loadStatic("merge");
    const form = document.querySelector("form");
    const fields = [...document.querySelectorAll(".merge-conflict")];

    expect(window.WikiMerge).toBeDefined();
    expect(document.querySelector("#merge-progress").textContent).toBe("해결 0 / 2");
    expect(submit(form).defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(fields[0].querySelector('[data-resolution="mine"]'));
    expect(document.querySelector("#editor").value).toBe(payload().mine);

    fields[0].querySelector('[data-resolution="current"]').click();
    expect(document.querySelector("#merge-progress").textContent).toBe("해결 1 / 2");
    expect(document.activeElement).toBe(fields[1].querySelector('[data-resolution="mine"]'));
    expect(fields[0].classList.contains("is-resolved")).toBe(true);
    expect(fields[0].querySelector('[data-resolution="current"]').getAttribute("aria-pressed")).toBe("true");

    const manual = fields[1].querySelector("textarea");
    manual.value = "manual two\n\n";
    fields[1].querySelector('[data-resolution="manual"]').click();
    expect(document.querySelector("#merge-progress").textContent).toBe("해결 2 / 2");
    expect(document.querySelector("#editor").value).toBe("head\ncurrent one\nmiddle\nmanual two\n\ntail\n");
    expect(document.querySelector("[data-merge-save]").disabled).toBe(false);
    expect(document.activeElement).toBe(document.querySelector("[data-merge-save]"));
    expect(submit(form).defaultPrevented).toBe(false);
  });

  test("writes through the existing editor view and can change a resolved choice", async () => {
    const data = payload({ conflicts: [payload().conflicts[0]], merged: "head\nrepeat\ntail\n" });
    const view = { state: { doc: { length: data.mine.length } }, dispatch: vi.fn() };
    page(data, { editorApi: { getView: vi.fn(() => view) } });
    await loadStatic("merge");
    const field = document.querySelector(".merge-conflict");

    field.querySelector('[data-resolution="mine"]').click();
    expect(view.dispatch).toHaveBeenLastCalledWith({ changes: { from: 0, to: data.mine.length, insert: "head\nmine one\ntail\n" } });
    field.querySelector('[data-resolution="current"]').click();
    expect(view.dispatch).toHaveBeenLastCalledWith({ changes: { from: 0, to: data.mine.length, insert: "head\ncurrent one\ntail\n" } });
  });

  test("falls back when the public editor API has no active view", async () => {
    const data = payload({ conflicts: [payload().conflicts[0]], merged: "head\nrepeat\ntail\n" });
    for (const editorApi of [{}, { getView: vi.fn(() => null) }]) {
      page(data, { editorApi });
      window.WikiMerge?.dispose();
      await loadStatic("merge");
      document.querySelector('[data-resolution="mine"]').click();
      expect(document.querySelector("#editor").value).toBe("head\nmine one\ntail\n");
    }
  });

  test("serializes empty insertion conflicts at bounded line offsets", async () => {
    const data = payload({
      base: "one\n",
      mine: "mine top\none\nmine end\n",
      current: "current top\none\ncurrent end\n",
      merged: "one\n",
      conflicts: [
        { start_line: 1, base: "", mine: "mine top\n", current: "current top\n", resolved: null },
        { start_line: 5, base: "", mine: "mine end\n", current: "current end\n", resolved: null },
      ],
    });
    page(data);
    await loadStatic("merge");
    document.querySelectorAll('[data-resolution="mine"]')[0].click();
    document.querySelectorAll('[data-resolution="current"]')[1].click();
    expect(document.querySelector("#editor").value).toBe("mine top\none\ncurrent end\n");
  });

  test("requires explicit proposal application and preserves trailing newlines", async () => {
    const data = payload({ conflicts: [], merged: "proposal\n\n", manual_only: false });
    page(data, { state: "proposal" });
    await loadStatic("merge");
    const form = document.querySelector("form");

    expect(submit(form).defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(document.querySelector("#apply-merge-proposal"));
    document.querySelector("#apply-merge-proposal").click();
    expect(document.querySelector("#editor").value).toBe("proposal\n\n");
    expect(document.querySelector("[data-merge-save]").disabled).toBe(false);
  });

  test("manual-only fallback keeps recovery editable without fake state", async () => {
    const data = payload({ manual_only: true, conflicts: [], merged: null });
    page(data, { state: "manual-only", saveDisabled: false });
    await loadStatic("merge");
    expect(document.querySelector("#editor").value).toBe(data.mine);
    expect(submit(document.querySelector("form")).defaultPrevented).toBe(false);
  });

  test("dispose and reinit consume a fresh second-conflict payload", async () => {
    page();
    await loadStatic("merge");
    const oldButton = document.querySelector('[data-resolution="mine"]');
    window.WikiMerge.dispose();
    oldButton.click();
    expect(document.querySelector("#merge-progress").textContent).toBe("해결 0 / 2");

    const fresh = payload({ mine: "second mine\n", merged: "second base\n", base: "second base\n",
      conflicts: [{ start_line: 1, base: "second base\n", mine: "second mine\n", current: "second current\n", resolved: null }], current_version: 8 });
    page(fresh);
    window.WikiMerge.init(document);
    document.querySelector('[data-resolution="current"]').click();
    expect(document.querySelector("#editor").value).toBe("second current\n");
    expect(document.querySelector('input[name="base_version"]').value).toBe("8");
  });

  test("safely ignores missing and malformed payloads", async () => {
    await loadStatic("merge");
    expect(window.WikiMerge.init()).toBeNull();
    document.body.innerHTML = '<section id="merge-resolver"></section><script id="merge-payload" type="application/json">{</script>';
    expect(window.WikiMerge.init()).toBeNull();
    document.querySelector("#merge-payload").textContent = "";
    expect(window.WikiMerge.init()).toBeNull();
    document.querySelector("#merge-payload").textContent = JSON.stringify(payload());
    expect(window.WikiMerge.init()).toBeNull();
  });
});
