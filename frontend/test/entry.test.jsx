import React, { act } from "react";
import { afterAll, beforeAll, beforeEach, describe, expect, test, vi } from "vitest";

const editor = vi.hoisted(() => ({ props: null, view: { name: "view" }, throwView: false }));
const configured = vi.hoisted(() => vi.fn());

vi.mock("md-editor-rt", async () => {
  const ReactModule = await import("react");
  return {
    config: configured,
    MdEditor: ReactModule.forwardRef(function FakeEditor(props, ref) {
      editor.props = props;
      ReactModule.useImperativeHandle(ref, () => ({
        getEditorView() {
          if (editor.throwView) throw new Error("view unavailable");
          return editor.view;
        },
      }));
      return ReactModule.createElement("div", { "data-testid": "editor" }, props.modelValue);
    }),
  };
});
vi.mock("highlight.js/lib/common", () => ({ default: { name: "highlight" } }));
vi.mock("cropperjs", () => ({ default: class Cropper {} }));

describe("WikiMdEditor global API", () => {
  let originalMatchMedia;

  beforeAll(async () => {
    originalMatchMedia = window.matchMedia;
    await import("../src/entry.jsx");
  });

  beforeEach(() => {
    editor.props = null;
    editor.throwView = false;
    window.matchMedia = vi.fn(() => ({ matches: false }));
  });

  afterAll(() => {
    window.matchMedia = originalMatchMedia;
  });

  test("configures offline dependencies and the wiki markdown preview", () => {
    expect(configured).toHaveBeenCalledOnce();
    const options = configured.mock.calls[0][0];
    expect(options.editorExtensions.highlight).toEqual({ instance: { name: "highlight" }, css: {} });
    expect(options.editorExtensions.cropper.css).toBe("");
    const md = { marker: false };
    options.markdownItConfig({
      inline: { ruler: { before: () => {} } },
      core: { ruler: { push: () => {} } },
      block: { ruler: { before: () => {} } },
      renderer: { rules: {} },
    });
  });

  test("mounts desktop editor defaults and exposes current value, view, theme, and save", async () => {
    document.documentElement.setAttribute("data-theme", "dark");
    const onChange = vi.fn();
    const onSave = vi.fn();
    const host = document.createElement("div");
    document.body.append(host);
    let api;
    await act(async () => {
      api = window.WikiMdEditor.mount(host, { initialValue: "start", onChange, onSave, height: "50vh" });
    });
    expect(editor.props).toMatchObject({
      modelValue: "start", theme: "dark", preview: true, language: "en-US",
      noKatex: true, noMermaid: true, noPrettier: true, noEcharts: true,
      noUploadImg: true, style: { height: "50vh" },
    });
    expect(editor.props.toolbars).toContain("pageFullscreen");
    expect(api.getView()).toBe(editor.view);
    await act(async () => editor.props.onChange("changed"));
    expect(api.getValue()).toBe("changed");
    expect(onChange).toHaveBeenCalledWith("changed");
    editor.props.onSave();
    expect(onSave).toHaveBeenCalledWith("changed");
    await act(async () => api.setTheme("light"));
    expect(editor.props.theme).toBe("light");
  });

  test("uses mobile edit-only mode and safe defaults when options are omitted", async () => {
    window.matchMedia = vi.fn(() => ({ matches: true }));
    const host = document.createElement("div");
    document.body.append(host);
    let api;
    await act(async () => { api = window.WikiMdEditor.mount(host); });
    expect(editor.props.preview).toBe(false);
    expect(editor.props.theme).toBe("light");
    expect(editor.props.style).toEqual({ height: "70vh" });
    expect(api.getValue()).toBe("");
    await act(async () => editor.props.onChange("quiet"));
    editor.props.onSave();
    expect(api.getValue()).toBe("quiet");
  });

  test("returns the initial value before React completes its first render", async () => {
    const firstHost = document.createElement("div");
    const secondHost = document.createElement("div");
    document.body.append(firstHost, secondHost);
    let firstApi;
    let secondApi;
    await act(async () => {
      firstApi = window.WikiMdEditor.mount(firstHost, { initialValue: "immediate" });
      expect(firstApi.getValue()).toBe("immediate");
      expect(typeof firstApi.setTheme).toBe("function");
      firstApi.setTheme("light");
      secondApi = window.WikiMdEditor.mount(secondHost, {});
      expect(secondApi.getValue()).toBe("");
    });
    expect(firstApi.getValue()).toBe("immediate");
    expect(secondApi.getValue()).toBe("");
  });

  test("uploads successful images in order while skipping empty and failed results", async () => {
    const uploadImage = vi.fn(async (file) => {
      if (file.name === "bad.png") throw new Error("upload failed");
      return file.name === "empty.png" ? null : `/media/${file.name}`;
    });
    const host = document.createElement("div");
    document.body.append(host);
    await act(async () => window.WikiMdEditor.mount(host, { uploadImage }));
    expect(editor.props.noUploadImg).toBe(false);
    const callback = vi.fn();
    await editor.props.onUploadImg([
      new File(["a"], "a.png"), new File([""], "empty.png"), new File(["x"], "bad.png"),
    ], callback);
    expect(callback).toHaveBeenCalledWith(["/media/a.png"]);
  });

  test("returns no image URLs without an uploader and shields unavailable views", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    let api;
    await act(async () => { api = window.WikiMdEditor.mount(host, {}); });
    const callback = vi.fn();
    await editor.props.onUploadImg([new File(["a"], "a.png")], callback);
    expect(callback).toHaveBeenCalledWith([]);
    editor.throwView = true;
    expect(api.getView()).toBeNull();
  });
});
