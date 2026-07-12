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
      { start_line: 2, base: "repeat\n", mine: "mine one\n", current: "current one\n", resolved: null, merged_start: 5 },
      { start_line: 4, base: "repeat\n", mine: "mine two\n", current: "current two\n", resolved: null, merged_start: 19 },
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
      <p id="merge-error" role="alert" hidden></p>
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
    const data = payload({ conflicts: [{ ...payload().conflicts[0], merged_start: 5 }], merged: "head\nrepeat\ntail\n" });
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
    const data = payload({ conflicts: [{ ...payload().conflicts[0], merged_start: 5 }], merged: "head\nrepeat\ntail\n" });
    for (const editorApi of [{}, { getView: vi.fn(() => null) }]) {
      page(data, { editorApi });
      window.WikiMerge?.dispose();
      await loadStatic("merge");
      document.querySelector('[data-resolution="mine"]').click();
      expect(document.querySelector("#editor").value).toBe("head\nmine one\ntail\n");
    }
  });

  test("inactive editor view makes the resolved textarea authoritative through submit", async () => {
    const data = payload({ conflicts: [{ ...payload().conflicts[0], merged_start: 5 }], merged: "head\nrepeat\ntail\n" });
    page(data);
    window.WIKI = { canWrite: false };
    const staleApi = {
      getValue: vi.fn(() => data.mine),
      getView: vi.fn(() => null),
      setTheme: vi.fn(),
    };
    window.WikiMdEditor = { mount: vi.fn(() => staleApi) };
    vi.stubGlobal("requestAnimationFrame", (callback) => callback());

    await loadStatic("editor");
    await loadStatic("merge");
    document.querySelector('[data-resolution="current"]').click();
    const textarea = document.querySelector("#editor");
    const mount = document.querySelector("#md-editor-mount");
    expect(textarea.value).toBe("head\ncurrent one\ntail\n");
    expect(textarea.hidden).toBe(false);
    expect(mount.hidden).toBe(true);

    document.querySelector("form").addEventListener("submit", (event) => event.preventDefault());
    document.querySelector("[data-merge-save]").click();
    expect(textarea.value).toBe("head\ncurrent one\ntail\n");
    expect(staleApi.getValue).not.toHaveBeenCalled();
  });

  test("serializes empty insertion conflicts at bounded line offsets", async () => {
    const data = payload({
      base: "one\n",
      mine: "mine top\none\nmine end\n",
      current: "current top\none\ncurrent end\n",
      merged: "one\n",
      conflicts: [
        { start_line: 1, base: "", mine: "mine top\n", current: "current top\n", resolved: null, merged_start: 0 },
        { start_line: 5, base: "", mine: "mine end\n", current: "current end\n", resolved: null, merged_start: 4 },
      ],
    });
    page(data);
    await loadStatic("merge");
    document.querySelectorAll('[data-resolution="mine"]')[0].click();
    document.querySelectorAll('[data-resolution="current"]')[1].click();
    expect(document.querySelector("#editor").value).toBe("mine top\none\ncurrent end\n");
  });

  test("uses the exact later repeated placeholder offset", async () => {
    const data = payload({
      base: "repeat\nanchor\nrepeat\ntail\n",
      mine: "repeat\nanchor\nMINE\ntail\n",
      current: "repeat\nanchor\nCURRENT\ntail\n",
      merged: "repeat\nanchor\nrepeat\ntail\n",
      conflicts: [{ start_line: 3, base: "repeat\n", mine: "MINE\n", current: "CURRENT\n", resolved: null, merged_start: 14 }],
    });
    page(data);
    await loadStatic("merge");
    document.querySelector('[data-resolution="current"]').click();
    expect(document.querySelector("#editor").value).toBe("repeat\nanchor\nCURRENT\ntail\n");
  });

  test("uses UTF-16 offsets for repeated emoji placeholders", async () => {
    const prefix = "😀\nrepeat🔥\nanchor\n";
    const data = payload({
      base: `${prefix}repeat🔥\ntail\n`,
      mine: `${prefix}MINE🧠\ntail\n`,
      current: `${prefix}CURRENT🚀\ntail\n`,
      merged: `${prefix}repeat🔥\ntail\n`,
      conflicts: [{
        start_line: 4,
        base: "repeat🔥\n",
        mine: "MINE🧠\n",
        current: "CURRENT🚀\n",
        resolved: null,
        merged_start: prefix.length,
      }],
    });
    page(data);
    await loadStatic("merge");
    document.querySelector('[data-resolution="current"]').click();
    expect(document.querySelector("#editor").value).toBe(`${prefix}CURRENT🚀\ntail\n`);
  });

  test("places an empty insertion after an earlier automatic edit", async () => {
    const data = payload({
      base: "head\nanchor\n",
      mine: "HEAD\nanchor\nmine\n",
      current: "head\nanchor\ncurrent\n",
      merged: "HEAD\nanchor\n",
      conflicts: [{ start_line: 3, base: "", mine: "mine\n", current: "current\n", resolved: null, merged_start: 12 }],
    });
    page(data);
    await loadStatic("merge");
    document.querySelector('[data-resolution="mine"]').click();
    expect(document.querySelector("#editor").value).toBe("HEAD\nanchor\nmine\n");
  });

  test.each([
    ["missing offset", (data) => { delete data.conflicts[0].merged_start; }],
    ["reversed offsets", (data) => { data.conflicts[1].merged_start = 4; }],
    ["out of range", (data) => { data.conflicts[0].merged_start = 999; }],
    ["base mismatch", (data) => { data.conflicts[0].merged_start = 0; }],
    ["negative offset", (data) => { data.conflicts[0].merged_start = -1; }],
    ["invalid hunk", (data) => { data.conflicts[0] = { mine: "mine" }; }],
    ["invalid start line", (data) => { data.conflicts[0].start_line = 0; }],
    ["invalid version", (data) => { data.current_version = -1; }],
    ["invalid manual flag", (data) => { data.manual_only = "false"; }],
    ["invalid base", (data) => { data.base = 42; }],
  ])("keeps save blocked for malformed payload: %s", async (_label, mutate) => {
    const data = payload();
    mutate(data);
    page(data);
    await loadStatic("merge");
    const error = document.querySelector("#merge-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("병합 데이터를 확인할 수 없습니다");
    expect(document.querySelector("[data-merge-save]").disabled).toBe(true);
    expect(submit(document.querySelector("form")).defaultPrevented).toBe(true);
    expect(document.querySelector("#editor").value).toBe(data.mine);
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
    const data = payload({ base: null, manual_only: true, conflicts: [], merged: null });
    page(data, { state: "manual-only", saveDisabled: false });
    await loadStatic("merge");
    expect(document.querySelector("#editor").value).toBe(data.mine);
    expect(submit(document.querySelector("form")).defaultPrevented).toBe(false);
  });

  test("manual-only remains keyboard editable and normally submittable without editor bundle", async () => {
    const data = payload({ base: null, manual_only: true, conflicts: [], merged: null });
    page(data, { state: "manual-only", saveDisabled: false });
    const textarea = document.querySelector("#editor");
    textarea.hidden = true;
    window.WIKI = { canWrite: true };
    delete window.WikiMdEditor;

    await loadStatic("editor");
    await loadStatic("merge");
    expect(textarea.hidden).toBe(false);
    expect(document.querySelector("#md-editor-mount").hidden).toBe(true);
    textarea.focus();
    textarea.value = "keyboard recovery";
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    const submitted = vi.fn((event) => event.preventDefault());
    document.querySelector("form").addEventListener("submit", submitted);
    document.querySelector("[data-merge-save]").click();
    expect(document.activeElement).toBe(textarea);
    expect(submitted).toHaveBeenCalledOnce();
    expect(textarea.value).toBe("keyboard recovery");
  });

  test("dispose and reinit consume a fresh second-conflict payload", async () => {
    page();
    await loadStatic("merge");
    const oldButton = document.querySelector('[data-resolution="mine"]');
    window.WikiMerge.dispose();
    oldButton.click();
    expect(document.querySelector("#merge-progress").textContent).toBe("해결 0 / 2");

    const fresh = payload({ mine: "second mine\n", merged: "second base\n", base: "second base\n",
      conflicts: [{ start_line: 1, base: "second base\n", mine: "second mine\n", current: "second current\n", resolved: null, merged_start: 0 }], current_version: 8 });
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
    expect(() => window.WikiMerge.init()).not.toThrow();
    expect(document.querySelector('[role="alert"]').hidden).toBe(false);
    document.querySelector("#merge-payload").remove();
    expect(() => window.WikiMerge.init()).not.toThrow();
    document.querySelector("#merge-resolver").insertAdjacentHTML(
      "afterend",
      `<script id="merge-payload" type="application/json">${JSON.stringify(payload())}</script>`
    );
    document.querySelector("#merge-payload").textContent = "";
    expect(() => window.WikiMerge.init()).not.toThrow();
    document.querySelector("#merge-payload").textContent = JSON.stringify(payload());
    expect(() => window.WikiMerge.init()).not.toThrow();
  });

  test.each([
    ["save", () => document.querySelector("[data-merge-save]").remove()],
    ["version", () => document.querySelector('input[name="base_version"]').remove()],
    ["textarea", () => document.querySelector("#editor").remove()],
    ["fieldset", () => document.querySelector(".merge-conflict:last-of-type").remove()],
    ["button", () => document.querySelector("[data-resolution]").remove()],
    ["index", () => { document.querySelector(".merge-conflict").dataset.conflictIndex = "9"; }],
    ["progress", () => document.querySelector("#merge-progress").remove()],
  ])("blocks partial resolver control shape: %s", async (_label, mutate) => {
    page();
    mutate();
    await loadStatic("merge");
    const error = document.querySelector("#merge-error");
    expect(error.hidden).toBe(false);
    expect(submit(document.querySelector("form")).defaultPrevented).toBe(true);
  });

  test.each([
    ["missing apply", () => document.querySelector("#apply-merge-proposal").remove()],
    ["invalid merged", () => {
      const node = document.querySelector("#merge-payload");
      const data = JSON.parse(node.textContent);
      data.merged = null;
      node.textContent = JSON.stringify(data);
    }],
  ])("blocks partial proposal shape: %s", async (_label, mutate) => {
    page(payload({ conflicts: [], merged: "proposal\n" }), { state: "proposal" });
    mutate();
    await loadStatic("merge");
    expect(document.querySelector("#merge-error").hidden).toBe(false);
    expect(submit(document.querySelector("form")).defaultPrevented).toBe(true);
  });

  test("blocks an unknown resolver mode and edits without a mount element", async () => {
    page();
    document.querySelector("#merge-resolver").dataset.mergeState = "unknown";
    await loadStatic("merge");
    expect(document.querySelector("#merge-error").hidden).toBe(false);

    page(payload({ conflicts: [{ ...payload().conflicts[0], merged_start: 5 }], merged: "head\nrepeat\ntail\n" }));
    document.querySelector("#md-editor-mount").remove();
    window.WikiMerge.init();
    document.querySelector('[data-resolution="mine"]').click();
    expect(document.querySelector("#editor").value).toBe("head\nmine one\ntail\n");
  });

  test("focuses the accessible error for invalid manual-only state without a save control", async () => {
    const data = payload({ conflicts: [], merged: null, manual_only: false });
    page(data, { state: "manual-only" });
    document.querySelector("[data-merge-save]").remove();
    await loadStatic("merge");
    submit(document.querySelector("form"));
    expect(document.activeElement).toBe(document.querySelector("#merge-error"));

    page(data, { state: "manual-only", saveDisabled: false });
    window.WikiMerge.init();
    expect(document.querySelector("#merge-error").hidden).toBe(false);
  });

  test.each(["conflicts", "proposal"])(
    "editor save without requestSubmit stays cancellable for %s",
    async (state) => {
      const data = state === "proposal"
        ? payload({ conflicts: [], merged: "proposal\n" })
        : payload({ conflicts: [{ ...payload().conflicts[0], merged_start: 5 }], merged: "head\nrepeat\ntail\n" });
      page(data, { state });
      window.WIKI = { canWrite: false };
      let options;
      let value = data.mine;
      const view = {
        state: { doc: { length: value.length } },
        dispatch(transaction) {
          value = transaction.changes.insert;
          this.state.doc.length = value.length;
          options.onChange(value);
        },
      };
      window.WikiMdEditor = {
        mount: vi.fn((_element, mountOptions) => {
          options = mountOptions;
          return { getValue: () => value, getView: () => view, setTheme: vi.fn() };
        }),
      };
      vi.stubGlobal("requestAnimationFrame", (callback) => callback());
      const form = document.querySelector("form");
      Object.defineProperty(form, "requestSubmit", { value: null, configurable: true });
      form.submit = vi.fn();
      const attempts = vi.fn();
      const allowed = vi.fn((event) => event.preventDefault());
      form.addEventListener("submit", attempts, true);
      form.addEventListener("submit", allowed);

      await loadStatic("editor");
      await loadStatic("merge");
      options.onSave(value);
      expect(attempts).toHaveBeenCalledOnce();
      expect(allowed).not.toHaveBeenCalled();
      expect(form.submit).not.toHaveBeenCalled();

      if (state === "proposal") document.querySelector("#apply-merge-proposal").click();
      else document.querySelector('[data-resolution="mine"]').click();
      options.onSave(value);
      expect(attempts).toHaveBeenCalledTimes(2);
      expect(allowed).toHaveBeenCalledOnce();
      expect(form.submit).not.toHaveBeenCalled();
    }
  );
});
