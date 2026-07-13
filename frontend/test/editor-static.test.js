import { beforeEach, describe, expect, test, vi } from "vitest";
import { flush, getObservers, loadStatic, useStaticIsolation } from "./static-test-utils.js";

let mountOptions;
let api;
let view;
let value;

useStaticIsolation();

function makeView(text = "", head = text.length) {
  let docText = text;
  const doc = {
    get length() { return docText.length; },
    sliceString: (from, to) => docText.slice(from, to),
    lineAt(pos) {
      const from = docText.lastIndexOf("\n", pos - 1) + 1;
      const end = docText.indexOf("\n", pos);
      return { from, to: end === -1 ? docText.length : end };
    },
  };
  return {
    state: { doc, selection: { main: { head } } },
    coordsAtPos: vi.fn(() => ({ left: 10.4, bottom: 20.6 })),
    dispatch: vi.fn((transaction) => {
      const { from, to, insert } = transaction.changes;
      docText = docText.slice(0, from) + insert + docText.slice(to);
      if (transaction.selection) view.state.selection.main.head = transaction.selection.anchor;
      value = docText;
      mountOptions.onChange(docText);
    }),
    focus: vi.fn(),
    text: () => docText,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  mountOptions = null;
  value = "initial";
  view = makeView(value);
  api = {
    getValue: vi.fn(() => value),
    getView: vi.fn(() => view),
    setTheme: vi.fn(),
  };
  window.WikiMdEditor = {
    mount: vi.fn((element, options) => {
      mountOptions = options;
      return api;
    }),
  };
  vi.stubGlobal("requestAnimationFrame", (callback) => callback());
});

function editorPage({ csrf = "token", cancel = true, conflict = false, counters = true } = {}) {
  document.body.innerHTML = `
    <form class="editform">${csrf === null ? "" : `<input name="csrf_token" value="${csrf}">`}
      <textarea id="editor">initial</textarea><div id="md-editor-mount"></div><button type="submit">save</button>
    </form>
    ${cancel ? '<a id="cancel-edit" href="/">cancel</a>' : ""}
    ${conflict ? '<button id="load-current">load</button><pre id="server-current">server text</pre>' : ""}
    ${counters ? '<span id="sb-words"></span><span id="sb-chars"></span>' : ""}`;
}

async function boot(options = {}, wiki = {}) {
  editorPage(options);
  window.WIKI = { canWrite: true, ...wiki };
  await loadStatic("editor");
  return document.querySelector("form");
}

function event(target, type, init = {}) {
  const e = type.startsWith("key")
    ? new KeyboardEvent(type, { bubbles: true, cancelable: true, ...init })
    : new Event(type, { bubbles: true, cancelable: true });
  target.dispatchEvent(e);
  return e;
}

function autocompleteText(text) {
  value = text;
  view = makeView(text);
  api.getView.mockImplementation(() => view);
  event(document.querySelector("#md-editor-mount"), "input");
  vi.advanceTimersByTime(100);
}

describe("editor.js", () => {
  test("wires new-document location fields before editor guards", async () => {
    document.body.innerHTML = `
      <input id="loc-folder" value=" /folder/sub/ "><input id="loc-name" value=" Note.MD ">
      <input id="loc-path"><span id="loc-preview"></span>`;
    await loadStatic("editor");
    expect(document.querySelector("#loc-path").value).toBe("folder/sub/Note.md");
    expect(document.querySelector("#loc-preview").textContent).toBe("생성 위치 · folder/sub/Note.md");
    const folder = document.querySelector("#loc-folder");
    const name = document.querySelector("#loc-name");
    folder.value = "";
    name.value = "";
    event(folder, "input");
    expect(document.querySelector("#loc-path").value).toBe("");
    expect(document.querySelector("#loc-preview").textContent).toBe("");

    for (const html of [
      '<input id="loc-name"><input id="loc-path">',
      '<input id="loc-folder"><input id="loc-path">',
      '<input id="loc-folder"><input id="loc-name">',
    ]) {
      document.body.innerHTML = html;
      await loadStatic("editor");
    }
  });

  test("focuses an empty name and works without a preview", async () => {
    document.body.innerHTML = '<input id="loc-folder"><input id="loc-name"><input id="loc-path">';
    const name = document.querySelector("#loc-name");
    await loadStatic("editor");
    expect(document.activeElement).toBe(name);
    name.value = "x";
    event(name, "input");
    expect(document.querySelector("#loc-path").value).toBe("x.md");
  });

  test("reloads the new-document form with every template and location shape", async () => {
    const cases = [
      `
        <input id="loc-folder" value=" /notes/ "><input id="loc-name" value=" Daily.md ">
        <select id="doc-template"><option value="daily" selected>daily</option></select>`,
      `
        <input id="loc-folder" value=""><input id="loc-name" value="Loose">
        <select id="doc-template"><option value="" selected>none</option></select>`,
      '<select id="doc-template"><option value="" selected>none</option></select>',
    ];
    for (const html of cases) {
      document.body.innerHTML = html;
      await loadStatic("editor");
      event(document.querySelector("#doc-template"), "change");
    }
  });

  test("requires form and textarea while falling back without mount or bundle", async () => {
    for (const html of [
      "", '<form class="editform"></form>',
    ]) {
      document.body.innerHTML = html;
      await loadStatic("editor");
    }
    editorPage();
    delete window.WikiMdEditor;
    await loadStatic("editor");
    expect(mountOptions).toBeNull();
    expect(document.querySelector("#editor").hidden).toBe(false);
    expect(document.querySelector("#md-editor-mount").hidden).toBe(true);

    document.body.innerHTML = '<form class="editform"><textarea id="editor" hidden>draft</textarea><button type="submit">save</button></form>';
    await loadStatic("editor");
    expect(document.querySelector("#editor").hidden).toBe(false);
  });

  test("visible textarea fallback edits, loads current, counts, and submits normally", async () => {
    editorPage({ conflict: true });
    const textarea = document.querySelector("#editor");
    textarea.hidden = true;
    delete window.WikiMdEditor;
    await loadStatic("editor");

    expect(textarea.hidden).toBe(false);
    expect(textarea.matches(":disabled")).toBe(false);
    textarea.focus();
    textarea.value = "keyboard draft";
    event(textarea, "input");
    expect(document.activeElement).toBe(textarea);
    expect(document.querySelector("#sb-words").textContent).toBe("2 단어");
    expect(event(window, "beforeunload").defaultPrevented).toBe(true);
    document.querySelector("#load-current").click();
    expect(textarea.value).toBe("server text");

    const submitted = vi.fn((e) => e.preventDefault());
    document.querySelector("form").addEventListener("submit", submitted);
    document.querySelector('button[type="submit"]').click();
    expect(submitted).toHaveBeenCalledOnce();
  });

  test.each([
    ["null", null],
    ["empty", {}],
    ["missing getView", { getValue: () => "stale", setTheme: () => {} }],
    ["missing getValue", { getView: () => null, setTheme: () => {} }],
    ["missing setTheme", { getValue: () => "stale", getView: () => null }],
  ])("uses the authoritative textarea when mount API is %s", async (_label, candidate) => {
    editorPage();
    const textarea = document.querySelector("#editor");
    textarea.hidden = true;
    window.WikiMdEditor.mount.mockReturnValue(candidate);

    await loadStatic("editor");

    const mount = document.querySelector("#md-editor-mount");
    expect(textarea.hidden).toBe(false);
    expect(mount.hidden).toBe(true);
    expect(typeof mount.wikiUseTextareaFallback).toBe("function");
    expect(mount.wikiEditorApi.getValue()).toBe("initial");
    expect(mount.wikiEditorApi.getView()).toBeNull();
  });

  test("mounts with theme, CSRF precedence, changes and CJK-aware counts", async () => {
    document.documentElement.setAttribute("data-theme", "dark");
    await boot({}, { csrf: "fallback" });
    expect(window.WikiMdEditor.mount).toHaveBeenCalledWith(document.querySelector("#md-editor-mount"), expect.any(Object));
    expect(mountOptions.initialValue).toBe("initial");
    expect(mountOptions.theme).toBe("dark");
    expect(typeof mountOptions.uploadImage).toBe("function");
    mountOptions.onChange("한글 test words");
    expect(document.querySelector("#editor").value).toBe("한글 test words");
    expect(document.querySelector("#sb-words").textContent).toBe("4 단어");
    expect(document.querySelector("#sb-chars").textContent).toBe("13 자");

    document.querySelector("#sb-chars").remove();
    mountOptions.onChange("word");
    expect(document.querySelector("#sb-words").textContent).toBe("1 단어");
    document.body.insertAdjacentHTML("beforeend", '<span id="sb-chars"></span>');
    document.querySelector("#sb-words").remove();
    mountOptions.onChange(null);
    expect(document.querySelector("#sb-chars").textContent).toBe("0 자");
    document.querySelector("#sb-chars").remove();
    mountOptions.onChange("ignored");
  });

  test("uses fallback or empty CSRF and disables writing features for viewers", async () => {
    await boot({ csrf: null }, { canWrite: false, csrf: "fallback" });
    expect(mountOptions.theme).toBe("light");
    expect(mountOptions.uploadImage).toBeNull();
    expect(document.querySelector(".wiki-ac")).toBeNull();
    await boot({ csrf: null }, { canWrite: true });
    expect(typeof mountOptions.uploadImage).toBe("function");
    editorPage({ csrf: null });
    delete window.WIKI;
    await loadStatic("editor");
    expect(mountOptions.uploadImage).toBeNull();
  });

  test("uploads images and exposes every server error shape", async () => {
    await boot();
    const file = new File(["image"], "image.png", { type: "image/png" });
    const replies = [
      { ok: true, url: "/uploads/image.png", expected: "/uploads/image.png" },
      { ok: false, error: { message: "too large" }, expected: null, toast: "업로드 실패: too large" },
      { ok: false, error: "denied", expected: null, toast: "업로드 실패: denied" },
      { ok: false, expected: null, toast: "업로드 실패: 오류" },
    ];
    for (const reply of replies) {
      vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ json: () => Promise.resolve(reply) })));
      await expect(mountOptions.uploadImage(file)).resolves.toBe(reply.expected);
      const [, options] = fetch.mock.calls[0];
      expect(options).toMatchObject({ method: "POST", headers: { "X-CSRF-Token": "token" }, credentials: "same-origin" });
      expect(options.body.get("file")).toBe(file);
      if (reply.toast) expect(document.querySelector(".rt-toast:last-of-type").textContent).toBe(reply.toast);
    }
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
    await expect(mountOptions.uploadImage(file)).resolves.toBeNull();
    expect(document.querySelector(".rt-toast:last-of-type").textContent).toBe("업로드 실패");
    vi.advanceTimersByTime(3000);
    vi.advanceTimersByTime(300);
    expect(document.querySelectorAll(".rt-toast")).toHaveLength(0);
  });

  test("saves through requestSubmit and a cancellable submit-button fallback", async () => {
    let form = await boot();
    form.requestSubmit = vi.fn(() => event(form, "submit"));
    value = "saved";
    mountOptions.onSave("save callback");
    expect(form.requestSubmit).toHaveBeenCalledOnce();
    expect(document.querySelector("#editor").value).toBe("saved");
    const unload = event(window, "beforeunload");
    expect(unload.defaultPrevented).toBe(false);

    form = await boot();
    Object.defineProperty(form, "requestSubmit", { value: null, configurable: true });
    form.submit = vi.fn();
    const submitted = vi.fn((e) => e.preventDefault());
    form.addEventListener("submit", submitted);
    mountOptions.onSave("fallback");
    expect(submitted).toHaveBeenCalledOnce();
    expect(form.submit).not.toHaveBeenCalled();

    form.querySelector('button[type="submit"]').remove();
    submitted.mockClear();
    mountOptions.onSave("event fallback");
    expect(submitted).toHaveBeenCalledOnce();
  });

  test("warns on dirty navigation and respects both cancel decisions", async () => {
    await boot();
    value = "changed";
    const unload = event(window, "beforeunload");
    expect(unload.defaultPrevented).toBe(true);
    expect(unload.returnValue).toBe(false);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const cancel = document.querySelector("#cancel-edit");
    expect(event(cancel, "click").defaultPrevented).toBe(true);
    confirm.mockReturnValue(true);
    expect(event(cancel, "click").defaultPrevented).toBe(false);

    await boot({ cancel: false });
    expect(document.querySelector("#cancel-edit")).toBeNull();
  });

  test("loads conflict text through CodeMirror and resets the dirty baseline", async () => {
    await boot({ conflict: true });
    value = "changed";
    document.querySelector("#load-current").click();
    expect(view.dispatch).toHaveBeenCalledWith({ changes: { from: 0, to: 7, insert: "server text" } });
    expect(document.querySelector("#editor").value).toBe("server text");
    expect(document.querySelector("#sb-words").textContent).toBe("2 단어");
    expect(event(window, "beforeunload").defaultPrevented).toBe(false);
    expect(document.querySelector(".rt-toast").textContent).toContain("서버의 현재 내용");

    await boot({ conflict: true });
    api.getView.mockReturnValue(null);
    document.querySelector("#server-current").textContent = "";
    document.querySelector("#load-current").click();
    expect(document.querySelector("#editor").value).toBe("");
    await boot({ conflict: false });
    document.body.insertAdjacentHTML("beforeend", '<button id="load-current"></button>');
  });

  test("mirrors later theme mutations", async () => {
    await boot();
    const observer = getObservers("MutationObserver").at(-1);
    expect(observer.observe).toHaveBeenCalledWith(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    document.documentElement.setAttribute("data-theme", "dark");
    observer.callback();
    document.documentElement.setAttribute("data-theme", "light");
    observer.callback();
    expect(api.setTheme.mock.calls.map((call) => call[0])).toEqual(["dark", "light"]);
  });

  test("exposes the mounted editor API on its DOM mount for merge resolution", async () => {
    await boot();
    expect(document.querySelector("#md-editor-mount").wikiEditorApi).toBe(api);
  });

  test("scans wiki queries and rejects closed, nested or unpositioned contexts", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    await boot();
    const mount = document.querySelector("#md-editor-mount");
    for (const text of ["plain", "[[closed]]", "[[bad["]) {
      autocompleteText(text);
    }
    api.getView.mockReturnValue(null);
    event(mount, "input");
    view = makeView("[[query");
    view.coordsAtPos.mockReturnValue(null);
    api.getView.mockReturnValue(view);
    event(mount, "input");
    expect(fetch).not.toHaveBeenCalled();

    view.coordsAtPos.mockReturnValueOnce(null).mockReturnValueOnce({ left: 1, bottom: 2 });
    event(mount, "input");
    vi.advanceTimersByTime(100);
    expect(fetch).toHaveBeenCalledWith("/api/complete?q=query", { credentials: "same-origin" });
  });

  test("handles empty and failed autocomplete responses", async () => {
    const replies = [null, { ok: false }, { ok: true }, { ok: true, items: [] }];
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ json: () => Promise.resolve(replies.shift()) })));
    await boot();
    for (let i = 0; i < 4; i++) {
      autocompleteText(`[[q${i}`);
      await flush();
      expect(document.querySelector(".wiki-ac").hidden).toBe(true);
    }
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
    autocompleteText("[[offline");
    await flush();
    expect(document.querySelector(".wiki-ac").hidden).toBe(true);
  });

  test("navigates autocomplete by keyboard and inserts a missing closer", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ json: () => Promise.resolve({ ok: true, items: [
      { title: "Alpha", path: "alpha.md" }, { path: "beta.md" },
    ] }) })));
    await boot();
    autocompleteText("prefix [[be");
    await flush();
    const menu = document.querySelector(".wiki-ac");
    expect(menu.hidden).toBe(false);
    expect(menu.style.left).toBe("10px");
    expect(menu.style.top).toBe("25px");
    expect(menu.children[1].querySelector(".wiki-ac-title").textContent).toBe("beta.md");
    const mount = document.querySelector("#md-editor-mount");
    expect(event(mount, "keydown", { key: "x" }).defaultPrevented).toBe(false);
    expect(event(mount, "keydown", { key: "ArrowDown" }).defaultPrevented).toBe(true);
    event(mount, "keydown", { key: "ArrowDown" });
    event(mount, "keydown", { key: "ArrowUp" });
    event(mount, "keydown", { key: "ArrowUp" });
    expect(menu.children[0].classList.contains("active")).toBe(true);
    event(mount, "keydown", { key: "ArrowDown" });
    event(mount, "keydown", { key: "Enter" });
    expect(view.text()).toBe("prefix [[beta.md]]");
    expect(view.focus).toHaveBeenCalledOnce();
    expect(menu.hidden).toBe(true);
  });

  test("uses an existing closer and supports mouse, tab, escape and outside close", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ json: () => Promise.resolve({ ok: true, items: [{ title: "A", path: "a.md" }] }) })));
    await boot();
    autocompleteText("[[a]]");
    view.state.selection.main.head = 3;
    event(document.querySelector("#md-editor-mount"), "input");
    vi.advanceTimersByTime(100);
    await flush();
    const menu = document.querySelector(".wiki-ac");
    menu.firstElementChild.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    expect(view.text()).toBe("[[a.md]]");

    autocompleteText("[[x");
    await flush();
    event(document.querySelector("#md-editor-mount"), "keydown", { key: "Tab" });
    expect(view.text()).toBe("[[a.md]]");
    autocompleteText("[[x");
    await flush();
    event(document.querySelector("#md-editor-mount"), "keydown", { key: "Escape" });
    expect(menu.hidden).toBe(true);
    event(document.querySelector("#md-editor-mount"), "keydown", { key: "x" });

    autocompleteText("[[x");
    await flush();
    menu.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(menu.hidden).toBe(false);
    document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(menu.hidden).toBe(true);
  });

  test("closes safely when the editor disappears and scans ordinary keyup only", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ json: () => Promise.resolve({ ok: true, items: [{ path: "a.md" }] }) })));
    await boot();
    autocompleteText("[[x");
    await flush();
    api.getView.mockReturnValue(null);
    event(document.querySelector("#md-editor-mount"), "keydown", { key: "Enter" });
    expect(document.querySelector(".wiki-ac").hidden).toBe(true);
    event(document.querySelector("#md-editor-mount"), "keyup", { key: "ArrowDown" });
    event(document.querySelector("#md-editor-mount"), "keyup", { key: "z" });
  });

  test("does not allow an older autocomplete response to replace a newer query", async () => {
    const pending = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve, reject) => pending.push({ resolve, reject }))));
    await boot();
    autocompleteText("[[old");
    autocompleteText("[[new");
    pending[1].resolve({ json: () => Promise.resolve({ ok: true, items: [{ title: "New", path: "new.md" }] }) });
    await flush();
    pending[0].resolve({ json: () => Promise.resolve({ ok: true, items: [{ title: "Old", path: "old.md" }] }) });
    await flush();
    expect(document.querySelector(".wiki-ac-title").textContent).toBe("New");

    autocompleteText("[[older");
    autocompleteText("[[newest");
    pending[3].resolve({ json: () => Promise.resolve({ ok: true, items: [{ title: "Newest", path: "newest.md" }] }) });
    await flush();
    pending[2].reject(new Error("stale failure"));
    await flush();
    expect(document.querySelector(".wiki-ac-title").textContent).toBe("Newest");
  });

  test("cancels pending autocomplete before fetch on escape or outside pointer", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ json: () => Promise.resolve({ ok: true, items: [] }) })));
    await boot();
    view = makeView("[[escape");
    api.getView.mockReturnValue(view);
    const mount = document.querySelector("#md-editor-mount");
    event(mount, "input");
    event(mount, "keydown", { key: "Escape" });
    vi.advanceTimersByTime(100);
    expect(fetch).not.toHaveBeenCalled();

    view = makeView("[[outside");
    api.getView.mockReturnValue(view);
    event(mount, "input");
    document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    vi.advanceTimersByTime(100);
    expect(fetch).not.toHaveBeenCalled();
  });
});
