// Glue that exposes md-editor-rt to our (no-framework) page code as a plain
// global: window.WikiMdEditor.mount(el, opts). All page wiring (form save,
// CSRF, upload URL, theme source) lives in static/editor.js so it can be tuned
// without rebuilding this bundle.
//
// Offline: md-editor-rt normally pulls highlight.js / katex / mermaid / prettier
// from a CDN at runtime. We bundle highlight.js and inject it via config(), and
// disable the rest, so the editor needs no network.
import React from "react";
import { createRoot } from "react-dom/client";
import { MdEditor, config } from "md-editor-rt";
import "md-editor-rt/lib/style.css";
import hljs from "highlight.js/lib/common";
import "highlight.js/styles/github.css";
import Cropper from "cropperjs";
import "cropperjs/dist/cropper.css";
import { installWikiExtensions } from "./md-extensions.js";

config({
  editorExtensions: {
    // Provide the instance so no <script> is fetched; leave css empty so no CDN
    // <link> is injected either — colours come from the bundled github theme.
    highlight: { instance: hljs, css: {} },
    // Bundle the image cropper too (md-editor-rt preloads it from a CDN once image
    // upload is enabled) so the editor makes zero external requests.
    cropper: { instance: Cropper, css: "" },
  },
  // Bring the live preview in line with the server renderer: [[wikilinks]],
  // Obsidian callouts, and ==highlight==.
  markdownItConfig(md) {
    installWikiExtensions(md);
  },
  // NOTE: a [[ ]] CodeMirror-6 typeahead can't be added here — md-editor-rt bundles
  // its own copy of CodeMirror internally, so an externally-imported @codemirror
  // extension would be a second instance and fail to resolve. Wikilinks still
  // render in the preview and can be typed by hand.
});

// Toolbar kept to features that work fully offline (no katex/mermaid/prettier/
// screenfull-fullscreen). pageFullscreen is internal, so it stays.
const TOOLBARS = [
  "bold", "italic", "strikeThrough", "title", "quote", "-",
  "unorderedList", "orderedList", "task", "-",
  "codeRow", "code", "link", "image", "table", "-",
  "revoke", "next", "save", "=",
  "pageFullscreen", "preview", "previewOnly", "catalog",
];

function readTheme() {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

function mount(el, opts) {
  opts = opts || {};
  // On a narrow viewport the side-by-side editor+preview is unusably cramped (each pane
  // wraps mid-word), so start with the preview pane collapsed — edit-only, full width.
  // The toolbar's preview / previewOnly buttons still let a phone user view the rendered
  // output on demand. Desktop keeps the split. Matches the app-shell mobile breakpoint.
  const startPreview = !(typeof window !== "undefined" && typeof window.matchMedia === "function" &&
    window.matchMedia("(max-width: 860px)").matches);
  let editorRef = null;
  // Expose md-editor-rt's OWN CodeMirror view so page code can drive it (e.g.
  // the [[ ]] typeahead) without importing @codemirror separately — an external
  // import would be a second CM instance and fail to resolve.
  const api = {
    getValue: () => opts.initialValue || "",
    getView: () => {
      try { return editorRef && editorRef.current && editorRef.current.getEditorView(); }
      catch (e) { return null; }
    },
  };

  function App() {
    const [text, setText] = React.useState(opts.initialValue || "");
    const [theme, setTheme] = React.useState(opts.theme || readTheme());
    const ref = React.useRef(null);
    editorRef = ref;
    // Re-point the imperative getters at the latest render's state every render.
    api.getValue = () => text;
    api.setTheme = setTheme;

    return React.createElement(MdEditor, {
      ref,
      modelValue: text,
      theme,
      language: "en-US",
      codeTheme: "github",
      previewTheme: "default",
      preview: startPreview,
      toolbars: TOOLBARS,
      noKatex: true,
      noMermaid: true,
      noPrettier: true,
      noEcharts: true,
      noUploadImg: !opts.uploadImage,
      style: { height: opts.height || "70vh" },
      onChange: (v) => { setText(v); if (opts.onChange) opts.onChange(v); },
      onSave: () => { if (opts.onSave) opts.onSave(api.getValue()); },
      onUploadImg: async (files, callback) => {
        const urls = [];
        for (const f of files) {
          try {
            const u = opts.uploadImage ? await opts.uploadImage(f) : null;
            if (u) urls.push(u);
          } catch (e) { /* skip failed upload */ }
        }
        callback(urls);
      },
    });
  }

  createRoot(el).render(React.createElement(App));
  return api;
}

window.WikiMdEditor = { mount };
