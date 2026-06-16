// Markdown editor page wiring. The editor itself is md-editor-rt (React + CodeMirror 6),
// shipped as a vendored offline bundle that exposes window.WikiMdEditor.mount(el, opts).
// This file keeps all page concerns out of the bundle: form submit (CAS/base_version),
// CSRF, image upload, light/dark theme sync, and the status-bar word count.
(function () {
  "use strict";
  var form = document.querySelector(".editform");
  var textarea = document.getElementById("editor");
  var mountEl = document.getElementById("md-editor-mount");
  if (!form || !textarea || !mountEl || !window.WikiMdEditor) return;
  var W = window.WIKI || {};
  var csrf = (form.querySelector('input[name="csrf_token"]') || {}).value || W.csrf || "";

  function toast(msg) {
    var t = document.createElement("div");
    t.className = "rt-toast"; t.setAttribute("role", "status"); t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () { t.classList.remove("show"); setTimeout(function () { t.remove(); }, 300); }, 3000);
  }

  // Upload an image to /api/upload and resolve to its URL (md-editor-rt inserts it).
  function uploadImage(file) {
    var fd = new FormData(); fd.append("file", file);
    return fetch("/api/upload", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: fd, credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) return d.url;
        toast("업로드 실패: " + ((d && d.error && (d.error.message || d.error)) || "오류"));
        return null;
      })
      .catch(function () { toast("업로드 실패"); return null; });
  }

  function submit() { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }

  // ---- live word/char count into the shell status bar (mirrors util.word_count) ----
  // The status bar lives in a later DOM block than this (content-block) script, so
  // look the elements up lazily — they may not exist yet when this file first runs.
  var cjkRe = /[぀-ヿ㐀-䶿一-鿿가-힣豈-﫿]/g;
  function count(v) {
    var elW = document.getElementById("sb-words"), elC = document.getElementById("sb-chars");
    if (!elW && !elC) return;
    v = v || "";
    var cjk = (v.match(cjkRe) || []).length;
    var latin = v.replace(cjkRe, " ").split(/\s+/).filter(Boolean).length;
    if (elW) elW.textContent = (cjk + latin) + " 단어";
    if (elC) elC.textContent = v.length + " 자";
  }

  var api = window.WikiMdEditor.mount(mountEl, {
    initialValue: textarea.value,
    theme: document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light",
    uploadImage: W.canWrite ? uploadImage : null,
    onChange: function (v) { textarea.value = v; count(v); },
    onSave: function (v) { textarea.value = v; submit(); },
  });
  count(textarea.value);

  // Make sure the latest value reaches the form even if submit is triggered elsewhere.
  form.addEventListener("submit", function () { textarea.value = api.getValue(); });

  // Mirror the app's light/dark toggle into the editor.
  new MutationObserver(function () {
    api.setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light");
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
})();
